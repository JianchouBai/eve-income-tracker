import json
import logging
import re
import secrets
import sqlite3
from asyncio import Lock
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from app.auth_attempts import (
    ATTEMPT_LIFETIME,
    AuthorizationAttemptError,
    AuthorizationAttemptStore,
)
from app.config import Settings
from app.data_location_controller import DataLocationController, DataLocationError
from app.desktop_control import DesktopControl
from app.desktop_session import DesktopLaunchTokenStore, DesktopSessionError
from app.esi import EsiError
from app.lp_reporting import LpReportRow, build_lp_report
from app.lp_storage import LpRepository
from app.models import CharacterGroupRecord, CharacterRecord
from app.reporting import BEIJING, build_dashboard, classify_entry, parse_date_range
from app.security import TokenCipher
from app.sso import PROJECTS_SCOPE, SsoClient, SsoError
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    GroupNameConflictError,
    JournalRepository,
    UnknownCharacterError,
    UnknownGroupError,
)
from app.sync import SyncService
from app.version import APP_VERSION

PACKAGE_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
SQLITE_MAX_INTEGER = 2**63 - 1
_EPHEMERAL_SESSION_SECRET = secrets.token_urlsafe(48)
CLOCK_SYNC_MESSAGE = (
    "EVE 授权令牌尚未生效，请在 Windows“设置 → 时间和语言 → 日期和时间”"
    "中同步时间后重新授权。"
)
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

LP_MESSAGES = {
    "lp_not_covered": "部分日期尚无 LP 统计，请同步收益。",
    "lp_partial_start": "所选区间包含 LP 建立基线之前的时间。",
    "lp_scope_required": "LP 待授权，请为此角色追加军团项目读取权限。",
    "project_permission_denied": "无法读取军团项目，已保留 LP 历史。",
    "project_not_found": "未找到 LP 对账项目。",
    "project_ambiguous": "找到多个同名项目，无法确定统计来源。",
    "project_type_mismatch": "项目类型不是获取忠诚点。",
    "project_source_mismatch": "项目未限定 C 海军 LP。",
    "project_multiplier_unsupported": "项目贡献单位不受支持。",
    "reading_decreased": "LP 读数异常，已保留上次记录。",
    "tail_unavailable": "旧项目尾账无法读取，统计存在缺口。",
    "configuration_gap": "项目配置变更后已重新建立基线，统计存在缺口。",
    "project_limit_reached": "LP 项目或个人贡献已达上限，可能停止记录。",
    "project_stopped": "LP 项目已结束，可能停止记录。",
    "invalid_data": "LP 数据无效，已保留原有记录。",
    "invalid_pages": "无法完整读取 LP 项目列表。",
    "not_found": "LP 项目不可用，已保留原有记录。",
    "request_failed": "无法读取 LP 数据，请稍后重试。",
    "authorization_failed": "角色授权已失效，请重新授权。",
    "lp_sync_failed": "LP 同步失败，已保留原有记录。",
}


def _lp_error(code):
    if code not in LP_MESSAGES:
        code = "lp_sync_failed"
    return {"code": code, "message": LP_MESSAGES[code]}


def _lp_sync_payload(result):
    if result is None:
        return None
    return {
        "status": result.status
        if result.status
        in {
            "ok",
            "baseline",
            "failed",
            "unavailable",
            "anomaly",
            "authorization_required",
        }
        else "failed",
        "completed_at": result.completed_at.isoformat(),
        "baseline_created": result.baseline_created,
        "gross": _decimal(result.amounts.gross),
        "deduction": _decimal(result.amounts.deduction),
        "net": _decimal(result.amounts.net),
        "error": _lp_error(result.error_code) if result.error_code else None,
        "warnings": [_lp_error(code) for code in result.warnings],
    }


def _is_persisted_id(value: int) -> bool:
    return 1 <= value <= SQLITE_MAX_INTEGER


def _parse_persisted_id(value: str) -> int | None:
    if len(value) > 19 or re.fullmatch(r"[1-9][0-9]*", value) is None:
        return None
    parsed = int(value)
    return parsed if _is_persisted_id(parsed) else None


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
        if value is not None and not _is_persisted_id(value):
            raise ValueError("group_id must be a positive signed 64-bit integer")
        return value


@dataclass
class AppServices:
    characters: CharacterRepository
    groups: CharacterGroupRepository
    journal: JournalRepository
    cipher: TokenCipher | None
    sso: SsoClient | None
    sync: SyncService | None
    lp: LpRepository | None = None


@dataclass
class RuntimeContext:
    mode: Literal["development", "desktop"]
    attempts: AuthorizationAttemptStore
    launch_tokens: DesktopLaunchTokenStore
    data_location: DataLocationController | None = None
    desktop_control: DesktopControl | None = None


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
    logger.warning("EVE authorization failed: stage=%s error_code=%s", stage, code)


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


def serialize_lp(row: LpReportRow) -> dict:
    return {
        "gross": _decimal(row.amounts.gross),
        "deduction": _decimal(row.amounts.deduction),
        "net": _decimal(row.amounts.net),
        "has_coverage": row.coverage.has_coverage,
        "complete": row.coverage.complete,
        "started_at": row.coverage.started_at.isoformat()
        if row.coverage.started_at
        else None,
        "warnings": [_lp_error(code) for code in row.coverage.warnings],
    }


def _public_character(
    record: CharacterRecord, repository: LpRepository | None = None
) -> dict:
    tracks, problems = (), ()
    if repository is not None:
        history = repository.report_history(
            {record.character_id},
            record.tracking_started_at,
            record.tracking_started_at,
        )
        tracks = history.tracking
        problems = tuple(p for p in history.problems if p.ended_at is None)
    status = "ok" if tracks else "baseline_required"
    error = None
    if record.authorization_required or PROJECTS_SCOPE not in record.granted_scopes:
        status, error = "authorization_required", _lp_error("lp_scope_required")
    elif problems:
        status, error = "unavailable", _lp_error(problems[-1].code)
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
        "lp": {
            "status": status,
            "started_at": min(t.started_at for t in tracks).isoformat()
            if tracks
            else None,
            "last_synced_at": max(t.last_success_at for t in tracks).isoformat()
            if tracks
            else None,
            "error": error,
            "warnings": [_lp_error(p.code) for p in problems],
        },
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


def create_app(
    settings: Settings,
    services: AppServices,
    clock: Clock,
    runtime: RuntimeContext | None = None,
) -> FastAPI:
    issues = settings.configuration_issues()
    runtime = runtime or RuntimeContext(
        mode=settings.runtime_mode,
        attempts=AuthorizationAttemptStore(clock=clock.now_utc),
        launch_tokens=DesktopLaunchTokenStore(clock=clock.now_utc),
    )
    if runtime.mode != settings.runtime_mode:
        raise ValueError("runtime mode must match settings")

    if services.lp is None:
        services.lp = LpRepository(services.characters.database)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.characters.database.initialize()
        yield

    storage_lock = Lock()
    app = FastAPI(lifespan=lifespan)
    app.state.services = services
    app.state.runtime = runtime
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
        launch_tokens = request.query_params.getlist("launch_token")
        if launch_tokens:
            if runtime.mode != "desktop" or len(launch_tokens) != 1:
                raise WebError(401, "invalid_launch", "桌面启动凭据无效或已使用。")
            try:
                desktop_session_id = runtime.launch_tokens.consume(launch_tokens[0])
            except DesktopSessionError:
                raise WebError(
                    401,
                    "invalid_launch",
                    "桌面启动凭据无效或已使用。",
                ) from None
            request.session.clear()
            request.session["viewer_authenticated"] = True
            request.session["desktop_session_id"] = desktop_session_id
            request.session["csrf_token"] = secrets.token_urlsafe(32)
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "configuration_issues": issues,
                "authenticated": request.session.get("viewer_authenticated", False),
                "csrf_token": request.session.get("csrf_token", ""),
                "runtime_mode": runtime.mode,
                "app_version": APP_VERSION,
            },
        )

    def storage_controller(request: Request, *, mutate: bool = False):
        if runtime.mode != "desktop":
            raise WebError(404, "not_found", "页面不存在。")
        _require_viewer(request)
        session_id = request.session.get("desktop_session_id")
        if not isinstance(session_id, str) or not session_id:
            raise WebError(401, "authentication_required", "桌面会话无效，请重启应用。")
        if mutate:
            _require_csrf(request)
        if runtime.data_location is None:
            raise WebError(
                503, "storage_unavailable", "数据存储设置不可用，请重启应用。"
            )
        return runtime.data_location

    def storage_failure(error: Exception):
        messages = {
            "space_insufficient": (
                409,
                "目标可用空间不足，请选择其他目录或取消恢复副本。",
            ),
            "migration_pending": (409, "已有待处理迁移，请重启应用完成迁移。"),
            "migration_recovery_pending": (
                409,
                "迁移恢复尚未完成，请完全退出并重新启动应用；恢复完成前无法更改存储位置。",
            ),
            "cleanup_pending": (409, "原数据目录尚未清理，请重启应用后重试。"),
            "path_invalid": (400, "目录无效，请选择完整的本地目录。"),
            "path_not_writable": (400, "目录不可写，请检查权限或选择其他目录。"),
            "path_reparse": (400, "目录包含不受支持的链接，请选择其他目录。"),
            "path_unavailable": (400, "目录不可用，请连接磁盘并重新选择。"),
            "path_unsafe": (400, "此目录不能作为数据位置，请选择其他目录。"),
            "target_not_empty": (400, "目标目录非空，请选择其他目录。"),
        }
        code = error.code if isinstance(error, DataLocationError) else ""
        if code in messages:
            status, message = messages[code]
            raise WebError(status, code, message) from None
        raise WebError(
            500, "location_operation_failed", "无法完成数据存储操作，请检查目录后重试。"
        ) from None

    async def migration_payload(request: Request):
        # Parse only after mode/session/CSRF, including wrong media types and malformed JSON.
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise WebError(400, "invalid_request", "请求参数无效。")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        try:
            payload = json.loads(await request.body(), object_pairs_hook=unique_object)
            if not isinstance(payload, dict) or set(payload) != {
                "target_parent",
                "create_recovery",
            }:
                raise ValueError
            parent = payload["target_parent"]
            if (
                not isinstance(parent, str)
                or not parent.strip()
                or len(parent) > 32767
                or "\x00" in parent
                or type(payload["create_recovery"]) is not bool
            ):
                raise ValueError
            return Path(parent), payload["create_recovery"]
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise WebError(400, "invalid_request", "请求参数无效。") from None

    async def storage_operation(operation, *args, **kwargs):
        # One lock covers reads, estimates and mutations; native I/O never runs
        # on the ASGI loop, and unrelated routes need not wait for this lock.
        async with storage_lock:
            return await run_in_threadpool(operation, *args, **kwargs)

    @app.get("/api/desktop/data-location")
    async def data_location_status(request: Request):
        controller = storage_controller(request)
        try:
            status = await storage_operation(controller.status)
            recovery = status.recovery
            return {
                "current_path": str(status.current_path),
                "volume": status.volume,
                "usage": {
                    key: status.usage.get(key, 0)
                    for key in (
                        "database",
                        "logs",
                        "backups",
                        "webview",
                        "config",
                        "other",
                        "recovery",
                        "total",
                    )
                },
                "recovery": None
                if recovery is None
                else {
                    "created_at": recovery.created_at.isoformat(),
                    "size_bytes": recovery.size_bytes,
                    "successful_starts": recovery.successful_starts,
                    "retention_days": 7,
                    "required_starts": 3,
                },
                "pending_migration": status.pending_migration is not None,
                "migration_recovery_pending": status.migration_recovery_pending,
                "pending_cleanup": status.pending_cleanup is not None,
                "pending_cleanup_path": (
                    str(status.pending_cleanup_path)
                    if status.pending_cleanup_path is not None
                    else None
                ),
                "busy": storage_lock.locked(),
            }
        except Exception as error:  # noqa: BLE001 - public filesystem boundary.
            storage_failure(error)

    @app.post("/api/desktop/data-location/estimate")
    async def estimate_data_location(request: Request):
        controller = storage_controller(request, mutate=True)
        parent, recovery = await migration_payload(request)
        try:
            estimate = await storage_operation(
                controller.estimate, parent, create_recovery=recovery
            )
            return {
                "target_path": str(estimate.target_root),
                "copy_bytes": estimate.copy_bytes,
                "recovery_bytes": estimate.recovery_bytes,
                "free_bytes": estimate.free_bytes,
                "space_sufficient": estimate.free_bytes
                >= estimate.copy_bytes + estimate.recovery_bytes + 65536,
            }
        except Exception as error:  # noqa: BLE001 - public filesystem boundary.
            storage_failure(error)

    @app.post("/api/desktop/data-location/migrations", status_code=202)
    async def schedule_data_location(request: Request):
        controller = storage_controller(request, mutate=True)
        parent, recovery = await migration_payload(request)
        try:
            estimate = await storage_operation(
                controller.schedule_migration, parent, create_recovery=recovery
            )
            return {"scheduled": True, "target_path": str(estimate.target_root)}
        except Exception as error:  # noqa: BLE001 - public filesystem boundary.
            storage_failure(error)

    @app.delete("/api/desktop/data-location/recovery")
    async def delete_data_recovery(request: Request):
        controller = storage_controller(request, mutate=True)
        if await request.body():
            raise WebError(400, "invalid_request", "请求参数无效。")

        def delete_if_available():
            if controller.status().pending_migration is not None:
                raise DataLocationError("migration_pending")
            return controller.delete_recovery()

        try:
            return {"freed_bytes": await storage_operation(delete_if_available)}
        except Exception as error:  # noqa: BLE001 - public filesystem boundary.
            storage_failure(error)

    @app.get("/auth/login")
    async def login(request: Request):
        if runtime.mode != "development":
            raise WebError(404, "not_found", "页面不存在。")
        if issues or services.sso is None:
            raise WebError(503, "configuration_required", "请先完成应用配置。")
        owner_session_id = secrets.token_urlsafe(32)
        attempt = runtime.attempts.start(owner_session_id)
        request.session.clear()
        request.session["authorization_session_id"] = owner_session_id
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        try:
            url = await services.sso.build_authorization_url(
                attempt.state,
                attempt.challenge,
            )
        except Exception:  # noqa: BLE001 - never expose provider text or credentials.
            runtime.attempts.discard(attempt.attempt_id)
            request.session.pop("authorization_session_id", None)
            raise WebError(
                502, "authorization_failed", "无法开始 EVE 授权，请稍后重试。"
            ) from None
        return RedirectResponse(url, status_code=302)

    @app.post("/api/authorization-attempts")
    async def start_desktop_authorization(request: Request):
        if runtime.mode != "desktop":
            raise WebError(404, "not_found", "页面不存在。")
        _require_csrf(request)
        if issues or services.sso is None:
            raise WebError(503, "configuration_required", "请先完成应用配置。")
        owner_session_id = request.session.get("desktop_session_id")
        if not isinstance(owner_session_id, str):
            raise WebError(401, "authentication_required", "桌面会话无效，请重启应用。")
        attempt = runtime.attempts.start(owner_session_id)
        try:
            authorization_url = await services.sso.build_authorization_url(
                attempt.state,
                attempt.challenge,
            )
        except Exception:  # noqa: BLE001 - never expose provider text or credentials.
            runtime.attempts.discard(attempt.attempt_id)
            raise WebError(
                502, "authorization_failed", "无法开始 EVE 授权，请稍后重试。"
            ) from None
        return JSONResponse(
            {
                "attempt_id": attempt.attempt_id,
                "authorization_url": authorization_url,
                "expires_in_seconds": int(ATTEMPT_LIFETIME.total_seconds()),
            },
            status_code=201,
        )

    @app.get("/api/authorization-attempts/{attempt_id}")
    async def desktop_authorization_status(attempt_id: str, request: Request):
        if runtime.mode != "desktop":
            raise WebError(404, "not_found", "页面不存在。")
        _require_viewer(request)
        owner_session_id = request.session.get("desktop_session_id")
        if not isinstance(owner_session_id, str):
            raise WebError(401, "authentication_required", "桌面会话无效，请重启应用。")
        try:
            status = runtime.attempts.consume_status(attempt_id, owner_session_id)
        except AuthorizationAttemptError as error:
            if error.code == "attempt_owner_mismatch":
                raise WebError(403, "forbidden", "无权查看此授权状态。") from None
            raise WebError(
                404, "attempt_not_found", "授权请求不存在或已过期。"
            ) from None
        message = None
        if status.status == "failed":
            message = (
                CLOCK_SYNC_MESSAGE
                if status.error_code == "clock_out_of_sync"
                else "授权未完成，请重试。"
            )
        return {"status": status.status, "message": message}

    def desktop_callback_page(
        succeeded: bool, *, clock_error: bool = False
    ) -> HTMLResponse:
        if succeeded:
            title = "EVE 授权已完成"
            detail = "授权已安全完成，可以关闭此页面并返回应用。"
        else:
            title = "EVE 授权未完成"
            detail = (
                CLOCK_SYNC_MESSAGE
                if clock_error
                else "授权未完成，请关闭此页面并返回应用重试。"
            )
        document = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>"
            + title
            + "</title></head><body><main><h1>"
            + title
            + "</h1><p>"
            + detail
            + "</p></main></body></html>"
        )
        return HTMLResponse(
            document,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'",
            },
        )

    @app.get("/auth/callback")
    async def callback(request: Request):
        state_values = request.query_params.getlist("state")
        state = state_values[0] if len(state_values) == 1 else ""
        callback_session_id = (
            request.session.get("desktop_session_id")
            if runtime.mode == "desktop"
            else request.session.get("authorization_session_id")
        )
        if runtime.mode == "development" and not isinstance(callback_session_id, str):
            raise WebError(400, "invalid_state", "登录状态无效或已使用，请重新登录。")
        try:
            claimed = runtime.attempts.claim_callback(state or "", callback_session_id)
        except AuthorizationAttemptError:
            raise WebError(
                400, "invalid_state", "登录状态无效或已使用，请重新登录。"
            ) from None
        if runtime.mode == "development":
            request.session.pop("authorization_session_id", None)

        def callback_failure(error_code: str, status: int, message: str):
            runtime.attempts.fail(claimed.attempt_id, error_code)
            if runtime.mode == "desktop":
                return desktop_callback_page(
                    False, clock_error=error_code == "clock_out_of_sync"
                )
            raise WebError(status, "authorization_failed", message)

        if issues or services.sso is None or services.cipher is None:
            return callback_failure(
                "configuration_required",
                503,
                "请先完成应用配置。",
            )
        code_values = request.query_params.getlist("code")
        error_values = request.query_params.getlist("error")
        code = code_values[0] if len(code_values) == 1 else ""
        if not code or error_values:
            return callback_failure(
                "authorization_denied",
                400,
                "EVE 授权未完成，请重试。",
            )
        try:
            tokens = await services.sso.exchange_code(code, claimed.verifier)
        except Exception as error:  # noqa: BLE001 - log only safe diagnostics.
            _log_authorization_failure("token_exchange", error)
            return callback_failure(
                "token_exchange_failed",
                502,
                "EVE 授权验证失败，请重试。",
            )
        try:
            identity = await services.sso.verify_access_token(tokens.access_token)
        except Exception as error:  # noqa: BLE001 - log only safe diagnostics.
            _log_authorization_failure("token_verification", error)
            if isinstance(error, SsoError) and error.code == "immature_token":
                return callback_failure("clock_out_of_sync", 502, CLOCK_SYNC_MESSAGE)
            return callback_failure(
                "token_verification_failed",
                502,
                "EVE 授权验证失败，请重试。",
            )
        result = None
        try:
            authorized_at = clock.now_utc()
            if services.sync is not None:
                result = await services.sync.save_authorization_and_sync(
                    identity, tokens, authorized_at
                )
            else:
                services.characters.upsert_authorization(
                    identity.character_id,
                    identity.character_name,
                    services.cipher.encrypt(tokens.refresh_token),
                    authorized_at,
                    granted_scopes=identity.scopes,
                )
        except Exception:  # noqa: BLE001 - expose only a fixed save failure.
            logger.warning(
                "EVE authorization failed: stage=credential_save code=unexpected_error"
            )
            return callback_failure(
                "credential_save_failed",
                500,
                "无法保存 EVE 授权，请重试。",
            )

        target = (
            "/?status=sync_failed"
            if result is not None and result.status != "ok"
            else "/"
        )

        runtime.attempts.succeed(claimed.attempt_id)
        if runtime.mode == "desktop":
            return desktop_callback_page(True)
        request.session.clear()
        request.session["viewer_authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse(target, status_code=302)

    @app.post("/auth/logout")
    async def logout(request: Request):
        _require_csrf(request)
        request.session.clear()
        return RedirectResponse("/", status_code=302)

    def requested_group(request: Request) -> CharacterGroupRecord:
        raw_values = request.query_params.getlist("group_id")
        group_id = _parse_persisted_id(raw_values[0]) if len(raw_values) == 1 else None
        if group_id is None:
            raise WebError(400, "invalid_request", "角色组参数无效。")
        group = services.groups.get(group_id)
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
            parsed_ids = []
            for value in raw_ids:
                character_id = _parse_persisted_id(value)
                if character_id is None:
                    raise ValueError
                parsed_ids.append(character_id)
            selected = (
                set(parsed_ids)
                if parsed_ids
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
                _public_character(record, services.lp)
                for record in services.characters.list_all()
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
        if not _is_persisted_id(group_id):
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
        if not _is_persisted_id(group_id):
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
        if not _is_persisted_id(character_id):
            raise WebError(400, "invalid_request", "角色参数无效。")
        try:
            record = services.characters.set_group(character_id, payload.group_id)
        except UnknownCharacterError:
            raise WebError(404, "unknown_character", "所选角色不存在。") from None
        except UnknownGroupError:
            raise WebError(404, "unknown_group", "所选角色组不存在。") from None
        return {"character": _public_character(record, services.lp)}

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
        lp_history = services.lp.report_history(
            selected, period.start_utc, period.end_utc_exclusive
        )
        lp_report = build_lp_report(
            lp_history,
            selected,
            period,
            current_warnings={
                record.character_id: ("lp_scope_required",)
                for record in records
                if record.character_id in selected
                and (
                    record.authorization_required
                    or PROJECTS_SCOPE not in record.granted_scopes
                )
            },
        )
        return {
            "group_id": group.group_id,
            "group_name": group.name,
            "from": period.start.isoformat(),
            "to": period.end.isoformat(),
            "selected_ids": sorted(report.selected_ids),
            **_totals(report),
            "lp": serialize_lp(lp_report.total),
            "daily": [
                {
                    "date": row.date.isoformat(),
                    **_totals(row),
                    "lp": serialize_lp(lp_report.daily[row.date]),
                }
                for row in report.daily
            ],
            "categories": [
                {"category": row.category, **_totals(row)} for row in report.categories
            ],
            "characters": [
                {
                    "character_id": row.character_id,
                    "name": row.name,
                    **_totals(row),
                    "lp": serialize_lp(lp_report.characters[row.character_id]),
                }
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
        if not _is_persisted_id(character_id):
            raise WebError(400, "invalid_request", "角色参数无效。")
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
        return {
            "character": {
                **_public_character(record, services.lp),
                "enabled": payload.enabled,
            }
        }

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
                    "lp": _lp_sync_payload(row.lp),
                    "error": None
                    if row.status == "ok"
                    else _sync_error(row.error_code),
                }
                for row in result.results
            ]
        }

    return app
