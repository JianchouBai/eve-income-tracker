import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from uuid import UUID

import httpx

from app.config import Settings
from app.lp_models import MAX_LP, LpContribution, LpProject, require_count
from app.models import JournalEntry

ESI_BASE_URL = "https://esi.evetech.net/latest"
MAX_PAGES = 100


class EsiError(Exception):
    def __init__(self, code: str, user_message: str):
        self.code = code
        self.user_message = user_message
        super().__init__(user_message)


class EsiDataError(EsiError):
    pass


class EsiAuthorizationError(EsiError):
    pass


class EsiNotFoundError(EsiError):
    pass


class EsiProjectPermissionError(EsiError):
    pass


class EsiClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 1),
    ):
        self._settings = settings
        self._http = http
        self._sleeper = sleeper
        self._jitter = jitter

    async def fetch_wallet_journal(
        self, access_token: str, character_id: int
    ) -> list[JournalEntry]:
        if not self._settings._has_valid_user_agent():
            raise EsiError(
                "invalid_configuration", "ESI client configuration is invalid."
            )
        first = await self._get_page(access_token, character_id, 1)
        pages = self._parse_pages(first.headers.get("X-Pages"))
        entries = self._decode_entries(first.content, character_id)
        for page in range(2, pages + 1):
            response = await self._get_page(access_token, character_id, page)
            entries.extend(self._decode_entries(response.content, character_id))
        return entries

    async def _get_page(
        self, access_token: str, character_id: int, page: int
    ) -> httpx.Response:
        return await self._get_response(
            access_token,
            f"{ESI_BASE_URL}/characters/{character_id}/wallet/journal/",
            params={"page": page},
        )

    async def _get_response(
        self,
        access_token: str | None,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        if not self._settings._has_valid_user_agent():
            raise EsiError(
                "invalid_configuration", "ESI client configuration is invalid."
            )
        headers = {
            "User-Agent": self._settings.eve_user_agent,
            "X-Compatibility-Date": self._settings.esi_compatibility_date,
        }
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        for attempt in range(3):
            try:
                response = await self._http.get(
                    url,
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError:
                if attempt == 2:
                    raise EsiError(
                        "request_failed", "ESI wallet request failed."
                    ) from None
                await self._sleeper(self._backoff(attempt))
                continue
            if response.status_code == 403 and "/projects" in url:
                raise EsiProjectPermissionError(
                    "project_permission_denied", "ESI project access denied."
                )
            if response.status_code in {401, 403}:
                raise EsiAuthorizationError(
                    "authorization_failed", "ESI authorization failed."
                )
            if response.status_code in {420, 429}:
                if attempt == 2:
                    raise EsiError("request_failed", "ESI wallet request failed.")
                await self._sleeper(self._retry_after(response, attempt))
                continue
            if 500 <= response.status_code <= 599:
                if attempt == 2:
                    raise EsiError("request_failed", "ESI wallet request failed.")
                await self._sleeper(self._backoff(attempt))
                continue
            if response.status_code == 404:
                raise EsiNotFoundError("not_found", "ESI resource not found.")
            if response.is_error or response.status_code != 200:
                raise EsiError("request_failed", "ESI wallet request failed.")
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def _lp_json(response: httpx.Response) -> dict:
        try:
            data = json.loads(response.content, parse_float=Decimal)
            if not isinstance(data, dict):
                raise TypeError
            return data
        except (ValueError, TypeError, UnicodeDecodeError):
            raise EsiDataError("invalid_data", "ESI project data is invalid.") from None

    @staticmethod
    def _positive_id(value: int) -> int:
        if type(value) is not int or not 0 < value <= MAX_LP:
            raise EsiDataError("invalid_data", "ESI ID is invalid.")
        return value

    @staticmethod
    def _project_id(value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise EsiDataError("invalid_data", "ESI project ID is invalid.") from None

    @staticmethod
    def _lp_time(value: str) -> datetime:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timestamp requires timezone")
        return result.astimezone(UTC)

    @staticmethod
    def _lp_project(data: dict, corporation_id: int) -> LpProject:
        try:
            name, state = data["name"], data["state"]
            if (
                not isinstance(name, str)
                or not name
                or state
                not in {
                    "Unspecified",
                    "Active",
                    "Closed",
                    "Completed",
                    "Expired",
                    "Deleted",
                }
            ):
                raise ValueError
            progress = data["progress"]
            current, desired = progress["current"], progress["desired"]
            require_count(current)
            require_count(desired)
            config = data.get("configuration", {})
            kind = next(iter(config), "unknown") if len(config) == 1 else "unknown"
            source = None
            if kind == "earn_loyalty_point":
                corps = config[kind].get("corporations", [])
                source = tuple(
                    sorted({EsiClient._positive_id(x["corporation_id"]) for x in corps})
                )
            contribution = data.get("contribution", {})
            multiplier = contribution.get("submission_multiplier", 1)
            if type(multiplier) is not int and not isinstance(multiplier, Decimal):
                raise ValueError
            multiplier = Decimal(multiplier)
            if not multiplier.is_finite():
                raise ValueError
            limit = contribution.get("participation_limit")
            if limit is not None:
                require_count(limit)
            return LpProject(
                corporation_id,
                data["id"],
                name,
                state,
                source,
                multiplier,
                current,
                desired,
                limit,
                kind,
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise EsiDataError("invalid_data", "ESI project data is invalid.") from None

    async def fetch_character_corporation(self, character_id: int) -> int:
        cid = self._positive_id(character_id)
        response = await self._get_response(
            None, f"https://esi.evetech.net/characters/{cid}"
        )
        try:
            return self._positive_id(self._lp_json(response)["corporation_id"])
        except KeyError:
            raise EsiDataError(
                "invalid_data", "ESI character data is invalid."
            ) from None

    async def fetch_corporation_projects(
        self, access_token: str, corporation_id: int
    ) -> tuple[LpProject, ...]:
        cid = self._positive_id(corporation_id)
        cursor, seen, projects = "0", set(), []
        for _ in range(MAX_PAGES):
            if cursor in seen:
                raise EsiDataError("invalid_pages", "ESI project cursor is invalid.")
            seen.add(cursor)
            response = await self._get_response(
                access_token,
                f"https://esi.evetech.net/corporations/{cid}/projects",
                params={"state": "All", "limit": 100, "after": cursor},
            )
            data = self._lp_json(response)
            items = data.get("projects")
            if not isinstance(items, list):
                raise EsiDataError("invalid_data", "ESI project list is invalid.")
            projects.extend(self._lp_project(item, cid) for item in items)
            pagination = data.get("cursor", {})
            if not isinstance(pagination, dict):
                raise EsiDataError("invalid_pages", "ESI project cursor is invalid.")
            cursor = pagination.get("after")
            if not items or cursor is None:
                return tuple(projects)
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                raise EsiDataError("invalid_pages", "ESI project cursor is invalid.")
        raise EsiDataError("invalid_pages", "ESI project page limit exceeded.")

    async def fetch_project(
        self, access_token: str, corporation_id: int, project_id: str
    ) -> LpProject:
        cid, pid = self._positive_id(corporation_id), self._project_id(project_id)
        response = await self._get_response(
            access_token, f"https://esi.evetech.net/corporations/{cid}/projects/{pid}"
        )
        project = self._lp_project(self._lp_json(response), cid)
        if project.project_id != pid:
            raise EsiDataError("invalid_data", "ESI project identity mismatch.")
        return project

    async def fetch_project_contribution(
        self, access_token: str, corporation_id: int, project_id: str, character_id: int
    ) -> LpContribution:
        cid, pid, char = (
            self._positive_id(corporation_id),
            self._project_id(project_id),
            self._positive_id(character_id),
        )
        response = await self._get_response(
            access_token,
            f"https://esi.evetech.net/corporations/{cid}/projects/{pid}/contribution/{char}",
        )
        data = self._lp_json(response)
        try:
            modified = data.get("last_modified")
            return LpContribution(
                data["contributed"],
                self._lp_time(modified) if modified is not None else None,
            )
        except (ValueError, TypeError, KeyError):
            raise EsiDataError(
                "invalid_data", "ESI contribution data is invalid."
            ) from None

    def _backoff(self, attempt: int) -> float:
        return min(60, 2**attempt + self._jitter())

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value is not None:
            try:
                seconds = float(value)
                if seconds >= 0:
                    return min(60, seconds)
            except ValueError:
                try:
                    target = parsedate_to_datetime(value).astimezone(UTC)
                    return min(60, max(0, (target - datetime.now(UTC)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return self._backoff(attempt)

    @staticmethod
    def _parse_pages(value: str | None) -> int:
        if value is None:
            return 1
        try:
            pages = int(value)
        except ValueError:
            raise EsiDataError("invalid_pages", "ESI page count is invalid.") from None
        if str(pages) != value or not 1 <= pages <= MAX_PAGES:
            raise EsiDataError("invalid_pages", "ESI page count is invalid.")
        return pages

    @staticmethod
    def _decode_entries(content: bytes, character_id: int) -> list[JournalEntry]:
        try:
            payload = json.loads(content, parse_float=Decimal, parse_int=int)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise EsiDataError("invalid_data", "ESI wallet data is invalid.") from None
        if not isinstance(payload, list):
            raise EsiDataError("invalid_data", "ESI wallet data is invalid.")
        try:
            return [
                EsiClient._entry_from_payload(item, character_id) for item in payload
            ]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise EsiDataError("invalid_data", "ESI wallet data is invalid.") from None

    @staticmethod
    def _entry_from_payload(item: object, character_id: int) -> JournalEntry:
        if not isinstance(item, dict):
            raise TypeError
        journal_id = item["id"]
        amount = item["amount"]
        ref_type = item["ref_type"]
        parsed_date = datetime.fromisoformat(item["date"])
        if parsed_date.tzinfo is None or parsed_date.utcoffset() is None:
            raise ValueError
        date_utc = parsed_date.astimezone(UTC)
        if type(journal_id) is not int or journal_id <= 0:
            raise ValueError
        amount = EsiClient._decimal_value(amount)
        if not isinstance(ref_type, str) or not ref_type:
            raise ValueError
        return JournalEntry(
            journal_id=journal_id,
            character_id=character_id,
            date_utc=date_utc,
            amount=amount,
            ref_type=ref_type,
            tax=EsiClient._decimal_value(item.get("tax"), optional=True),
            balance=EsiClient._decimal_value(item.get("balance"), optional=True),
            description=item.get("description", ""),
            reason=item.get("reason"),
            context_id=item.get("context_id"),
            context_id_type=item.get("context_id_type"),
            first_party_id=item.get("first_party_id"),
            first_party_type=item.get("first_party_type"),
            second_party_id=item.get("second_party_id"),
            second_party_type=item.get("second_party_type"),
            tax_receiver_id=item.get("tax_receiver_id"),
        )

    @staticmethod
    def _decimal_value(value: object, optional: bool = False) -> Decimal | None:
        if value is None and optional:
            return None
        if isinstance(value, Decimal) and value.is_finite():
            return value
        if type(value) is int:
            return Decimal(value)
        raise ValueError
