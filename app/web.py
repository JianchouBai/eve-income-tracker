import logging
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings
from app.esi import EsiError
from app.models import CharacterGroupRecord, CharacterRecord
from app.reporting import BEIJING, build_dashboard, classify_entry, parse_date_range
from app.security import TokenCipher
from app.sso import SsoClient, SsoError
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    GroupNameConflictError,
    JournalRepository,
    UnknownCharacterError,
    UnknownGroupError,
)
from app.sync import SyncService

PACKAGE_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
_EPHEMERAL_SESSION_SECRET = secrets.token_urlsafe(48)
SAFE_SSO_ERROR_CODES = frozenset(
    {
        "expired_token",
        "immature_token",
        "invalid_audience_claim",
        "invalid_grant",
        "invalid_issuer",
        "invalid_jwks",
        "invalid_metadata",
        "invalid_name_claim",
        "invalid_response",
        "invalid_claims",
        "invalid_scope_format",
        "invalid_signature",
        "invalid_standard_claims",
        "invalid_subject_claim",
        "invalid_token",
        "malformed_token",
        "missing_required_claim",
        "missing_key_id",
        "request_failed",
        "signing_key_not_found",
        "unavailable",
        "unsupported_algorithm",
        "unsupported_es256",
        "wallet_scope_not_granted",
    }
)
SYNC_MESSAGES = {
    "authorization_failed": "角色授权已失效，请重新授权。",
    "rate_limited": "请求过于频繁，请稍后重试。",
    "invalid_data": "同步数据无效，已保留原有数据。",
    "request_failed": "无法获取 EVE 数据，请稍后重试。",
    "sync_failed": "同步失败，已保留原有数据。",
    "unknown_character": "所选角色不存在。",
    "invalid_configuration": "请先完成应用配置。",
}


class CharacterEnabledRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    enabled: bool


class CharacterGroupNameRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not 1 <= len(normalized) <= 50:
            raise ValueError("group name must contain 1 to 50 characters")
        return normalized


class CharacterGroupAssignmentRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    group_id: int | None

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("group_id must be positive")
        return value


@dataclass
class AppServices:
    characters: CharacterRepository
    groups: CharacterGroupRepository
    journal: JournalRepository
    cipher: TokenCipher | None
    sso: SsoClient | None
    sync: SyncService | None


class Clock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class WebError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _matches(actual: str | None, expected: str | None) -> bool:
    return bool(
        actual
        and expected
        and secrets.compare_digest(actual.encode(), expected.encode())
    )


def _log_authorization_failure(stage: str, error: Exception) -> None:
    code = (
        error.code
        if isinstance(error, SsoError) and error.code in SAFE_SSO_ERROR_CODES
        else "unexpected_error"
    )
    logger.warning("EVE authorization failed: stage=%s code=%s", stage, code)


def _require_viewer(request: Request) -> None:
    if request.session.get("viewer_authenticated") is not True:
        raise WebError(401, "authentication_required", "请先登录 EVE。")


def _require_csrf(request: Request) -> None:
    _require_viewer(request)
    if not _matches(
        request.headers.get("X-CSRF-Token"), request.session.get("csrf_token")
    ):
        raise WebError(403, "csrf_failed", "请求校验失败，请刷新页面后重试。")


def _decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _totals(value) -> dict:
    return {
        "income": _decimal(value.income),
        "tax": _decimal(value.tax),
        "net": _decimal(value.net),
    }


def _public_character(record: CharacterRecord) -> dict:
    return {
        "character_id": record.character_id,
        "name": record.name,
        "enabled": record.enabled,
        "authorization_required": record.authorization_required,
        "tracking_started_at": record.tracking_started_at.isoformat(),
        "last_synced_at": record.last_synced_at.isoformat()
        if record.last_synced_at
        else None,
        "group_id": record.group_id,
    }


def _public_group(record: CharacterGroupRecord, member_count: int) -> dict:
    return {
        "group_id": record.group_id,
        "name": record.name,
        "created_at": record.created_at.isoformat(),
        "member_count": member_count,
    }


def _date_param(raw: str | None, default: date) -> date:
    if raw is None:
        return default
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) is None:
        raise ValueError
    return date.fromisoformat(raw)


def _sync_error(code: str | None) -> dict:
    if code == "invalid_pages":
        code = "invalid_data"
    if code not in SYNC_MESSAGES:
        code = "sync_failed"
    return {"code": code, "message": SYNC_MESSAGES[code]}


def create_app(settings: Settings, services: AppServices, clock: Clock) -> FastAPI:
    issues = settings.configuration_issues()
    pending_states: set[str] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.characters.database.initialize()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.services = services
    try:
        callback = urlsplit(settings.eve_callback_url)
        local_http = callback.scheme == "http" and callback.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    except ValueError:
        local_http = False
    app.add_middleware(
        SessionMiddleware,
        secret_key=_EPHEMERAL_SESSION_SECRET if issues else settings.app_session_secret,
        same_site="lax",
        https_only=not local_http,
    )
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    static_path = PACKAGE_ROOT / "static"
    if static_path.is_dir():
        app.mount("/static", StaticFiles(directory=static_path), name="static")

    @app.exception_handler(WebError)
    async def web_error(request: Request, error: WebError):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=error.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return await web_error(
            request, WebError(400, "invalid_request", "请求参数无效。")
        )

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error: sqlite3.Error):
        return await web_error(
            request, WebError(500, "data_error", "数据访问失败，请稍后重试。")
        )

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def home(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "configuration_issues": issues,
                "authenticated": request.session.get("viewer_authenticated", False),
                "csrf_token": request.session.get("csrf_token", ""),
            },
        )

    @app.get("/auth/login")
    async def login(request: Request):
        if issues or services.sso is None:
            raise WebError(503, "configuration_required", "请先完成应用配置。")
        state = secrets.token_urlsafe(32)
        pending_states.discard(request.session.get("oauth_state"))
        pending_states.add(state)
        request.session["oauth_state"] = state
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        try:
            url = await services.sso.build_authorization_url(state)
        except Exception:  # noqa: BLE001 - never expose provider text or credentials.
            pending_states.discard(state)
            request.session.pop("oauth_state", None)
            raise WebError(
                502, "authorization_failed", "无法开始 EVE 授权，请稍后重试。"
            ) from None
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/callback")
    async def callback(request: Request):
        expected = request.session.pop("oauth_state", None)
        known = expected in pending_states
        pending_states.discard(expected)
        if not _matches(request.query_params.get("state"), expected) or not known:
            raise WebError(400, "invalid_state", "登录状态无效或已使用，请重新登录。")
        if issues or services.sso is None or services.cipher is None:
            raise WebError(503, "configuration_required", "请先完成应用配置。")
        code = request.query_params.get("code")
        if not code or request.query_params.get("error"):
            raise WebError(400, "authorization_failed", "EVE 授权未完成，请重试。")
        try:
            tokens = await services.sso.exchange_code(code)
        except Exception as error:  # noqa: BLE001 - log only safe diagnostics.
            _log_authorization_failure("token_exchange", error)
            raise WebError(
                502, "authorization_failed", "EVE 授权验证失败，请重试。"
            ) from None
        try:
            identity = await services.sso.verify_access_token(tokens.access_token)
        except Exception as error:  # noqa: BLE001 - log only safe diagnostics.
            _log_authorization_failure("token_verification", error)
            raise WebError(
                502, "authorization_failed", "EVE 授权验证失败，请重试。"
            ) from None
        authorized_at = clock.now_utc()
        services.characters.upsert_authorization(
            identity.character_id,
            identity.character_name,
            services.cipher.encrypt(tokens.refresh_token),
            authorized_at,
        )
        request.session["viewer_authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        target = "/"
        if services.sync is not None:
            try:
                result = await services.sync.sync_character(
                    identity.character_id, initial_access_token=tokens.access_token
                )
                if result.status != "ok":
                    target = "/?status=sync_failed"
            except Exception:  # noqa: BLE001 - authorization remains saved on sync failure.
                target = "/?status=sync_failed"
        return RedirectResponse(target, status_code=302)

    @app.post("/auth/logout")
    async def logout(request: Request):
        _require_csrf(request)
        pending_states.discard(request.session.get("oauth_state"))
        request.session.clear()
        return RedirectResponse("/", status_code=302)

    def requested_group(request: Request) -> CharacterGroupRecord:
        raw_values = request.query_params.getlist("group_id")
        if len(raw_values) != 1 or re.fullmatch(r"[1-9][0-9]*", raw_values[0]) is None:
            raise WebError(400, "invalid_request", "角色组参数无效。")
        group = services.groups.get(int(raw_values[0]))
        if group is None:
            raise WebError(404, "unknown_group", "所选角色组不存在。")
        return group

    def report_input(request: Request):
        _require_viewer(request)
        group = requested_group(request)
        records = services.characters.list_for_group(group.group_id)
        member_ids = {record.character_id for record in records}
        try:
            today = clock.now_utc().astimezone(BEIJING).date()
            end = _date_param(request.query_params.get("to"), today)
            start = _date_param(
                request.query_params.get("from"), end - timedelta(days=29)
            )
            period = parse_date_range(start, end)
            raw_ids = request.query_params.getlist("character_id")
            if any(re.fullmatch(r"[1-9][0-9]*", value) is None for value in raw_ids):
                raise ValueError
            selected = (
                {int(value) for value in raw_ids}
                if raw_ids
                else {record.character_id for record in records if record.enabled}
            )
        except (ValueError, OverflowError):
            raise WebError(
                400, "invalid_request", "日期或角色参数无效，日期范围最多为 366 天。"
            ) from None
        if selected - member_ids:
            raise WebError(
                400,
                "character_not_in_group",
                "所选角色不属于当前角色组。",
            )
        entries = services.journal.list_for_report(
            selected, period.start_utc, period.end_utc_exclusive
        )
        return group, records, member_ids, selected, period, entries

    @app.get("/api/characters")
    async def characters(request: Request):
        _require_viewer(request)
        return {
            "characters": [
                _public_character(record) for record in services.characters.list_all()
            ]
        }

    @app.get("/api/groups")
    async def groups(request: Request):
        _require_viewer(request)
        characters = services.characters.list_all()
        counts: dict[int, int] = {}
        ungrouped_count = 0
        for record in characters:
            if record.group_id is None:
                ungrouped_count += 1
            else:
                counts[record.group_id] = counts.get(record.group_id, 0) + 1
        return {
            "groups": [
                _public_group(record, counts.get(record.group_id, 0))
                for record in services.groups.list_all()
            ],
            "ungrouped_count": ungrouped_count,
        }

    @app.post("/api/groups", status_code=201, dependencies=[Depends(_require_csrf)])
    async def create_group(payload: CharacterGroupNameRequest):
        try:
            record = services.groups.create(payload.name, clock.now_utc())
        except GroupNameConflictError:
            raise WebError(400, "group_name_conflict", "角色组名称已存在。") from None
        return {"group": _public_group(record, 0)}

    @app.patch("/api/groups/{group_id}", dependencies=[Depends(_require_csrf)])
    async def rename_group(group_id: int, payload: CharacterGroupNameRequest):
        if group_id <= 0:
            raise WebError(400, "invalid_request", "角色组参数无效。")
        try:
            record = services.groups.rename(group_id, payload.name)
        except GroupNameConflictError:
            raise WebError(400, "group_name_conflict", "角色组名称已存在。") from None
        except UnknownGroupError:
            raise WebError(404, "unknown_group", "所选角色组不存在。") from None
        return {
            "group": _public_group(
                record, len(services.characters.list_for_group(group_id))
            )
        }

    @app.delete("/api/groups/{group_id}", dependencies=[Depends(_require_csrf)])
    async def delete_group(group_id: int):
        if group_id <= 0:
            raise WebError(400, "invalid_request", "角色组参数无效。")
        try:
            moved_character_ids = services.groups.delete(group_id)
        except UnknownGroupError:
            raise WebError(404, "unknown_group", "所选角色组不存在。") from None
        return {"moved_character_ids": list(moved_character_ids)}

    @app.post(
        "/api/characters/{character_id}/group",
        dependencies=[Depends(_require_csrf)],
    )
    async def assign_character_group(
        character_id: int, payload: CharacterGroupAssignmentRequest
    ):
        if character_id <= 0:
            raise WebError(400, "invalid_request", "角色参数无效。")
        try:
            record = services.characters.set_group(character_id, payload.group_id)
        except UnknownCharacterError:
            raise WebError(404, "unknown_character", "所选角色不存在。") from None
        except UnknownGroupError:
            raise WebError(404, "unknown_group", "所选角色组不存在。") from None
        return {"character": _public_character(record)}

    @app.get("/api/dashboard")
    async def dashboard(request: Request):
        group, records, member_ids, selected, period, entries = report_input(request)
        report = build_dashboard(
            entries,
            records,
            selected,
            period,
            group_member_ids=member_ids,
        )
        return {
            "group_id": group.group_id,
            "group_name": group.name,
            "from": period.start.isoformat(),
            "to": period.end.isoformat(),
            "selected_ids": sorted(report.selected_ids),
            **_totals(report),
            "daily": [
                {"date": row.date.isoformat(), **_totals(row)} for row in report.daily
            ],
            "categories": [
                {"category": row.category, **_totals(row)} for row in report.categories
            ],
            "characters": [
                {"character_id": row.character_id, "name": row.name, **_totals(row)}
                for row in report.characters
            ],
        }

    @app.get("/api/entries")
    async def journal_entries(request: Request):
        group, records, member_ids, selected, period, entries = report_input(request)
        names = {record.character_id: record.name for record in records}
        rows = []
        for entry in entries:
            classified = classify_entry(entry, member_ids)
            if classified is None:
                continue
            rows.append(
                {
                    "journal_id": entry.journal_id,
                    "character_id": entry.character_id,
                    "character_name": names[entry.character_id],
                    "date_utc": entry.date_utc.isoformat(),
                    "date": entry.date_utc.astimezone(BEIJING).date().isoformat(),
                    "ref_type": entry.ref_type,
                    "kind": classified.kind,
                    "category": classified.category,
                    "amount": _decimal(entry.amount),
                    "description": entry.description,
                }
            )
        return {
            "group_id": group.group_id,
            "group_name": group.name,
            "from": period.start.isoformat(),
            "to": period.end.isoformat(),
            "selected_ids": sorted(selected),
            "entries": rows,
        }

    @app.post(
        "/api/characters/{character_id}/enabled", dependencies=[Depends(_require_csrf)]
    )
    async def set_enabled(character_id: int, payload: CharacterEnabledRequest):
        record = next(
            (
                record
                for record in services.characters.list_all()
                if record.character_id == character_id
            ),
            None,
        )
        if record is None:
            raise WebError(404, "unknown_character", "所选角色不存在。")
        services.characters.set_enabled(character_id, payload.enabled)
        return {"character": {**_public_character(record), "enabled": payload.enabled}}

    @app.post("/api/sync", dependencies=[Depends(_require_csrf)])
    async def synchronize(request: Request):
        group = requested_group(request)
        if issues or services.sync is None:
            raise WebError(503, "configuration_required", "请先完成应用配置。")
        try:
            result = await services.sync.sync_group(group.group_id)
        except Exception as error:  # noqa: BLE001 - external service boundary.
            code = (
                error.code if isinstance(error, (EsiError, SsoError)) else "sync_failed"
            )
            public_error = _sync_error(code)
            status = 429 if public_error["code"] == "rate_limited" else 502
            raise WebError(
                status, public_error["code"], public_error["message"]
            ) from None
        return {
            "results": [
                {
                    "character_id": row.character_id,
                    "new_count": row.new_count,
                    "completed_at": row.completed_at.isoformat(),
                    "status": "ok" if row.status == "ok" else "failed",
                    "error": None
                    if row.status == "ok"
                    else _sync_error(row.error_code),
                }
                for row in result.results
            ]
        }

    return app
