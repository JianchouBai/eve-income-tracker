import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx

from app.config import Settings
from app.models import JournalEntry

ESI_BASE_URL = 'https://esi.evetech.net/latest'
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
        first = await self._get_page(access_token, character_id, 1)
        pages = self._parse_pages(first.headers.get('X-Pages'))
        entries = self._decode_entries(first.content, character_id)
        for page in range(2, pages + 1):
            response = await self._get_page(access_token, character_id, page)
            entries.extend(self._decode_entries(response.content, character_id))
        return entries

    async def _get_page(
        self, access_token: str, character_id: int, page: int
    ) -> httpx.Response:
        for attempt in range(3):
            try:
                response = await self._http.get(
                    f'{ESI_BASE_URL}/characters/{character_id}/wallet/journal/',
                    params={'page': page},
                    headers={
                        'Authorization': f'Bearer {access_token}',
                        'User-Agent': self._settings.eve_user_agent,
                        'X-Compatibility-Date': self._settings.esi_compatibility_date,
                    },
                )
            except httpx.HTTPError:
                if attempt == 2:
                    raise EsiError('request_failed', 'ESI wallet request failed.') from None
                await self._sleeper(self._backoff(attempt))
                continue
            if response.status_code in {401, 403}:
                raise EsiAuthorizationError('authorization_failed', 'ESI authorization failed.')
            if response.status_code in {420, 429}:
                if attempt == 2:
                    raise EsiError('request_failed', 'ESI wallet request failed.')
                await self._sleeper(self._retry_after(response, attempt))
                continue
            if 500 <= response.status_code <= 599:
                if attempt == 2:
                    raise EsiError('request_failed', 'ESI wallet request failed.')
                await self._sleeper(self._backoff(attempt))
                continue
            if response.is_error:
                raise EsiError('request_failed', 'ESI wallet request failed.')
            return response
        raise AssertionError('unreachable')

    def _backoff(self, attempt: int) -> float:
        return min(60, 2**attempt + self._jitter())

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get('Retry-After')
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
            raise EsiDataError('invalid_pages', 'ESI page count is invalid.') from None
        if str(pages) != value or not 1 <= pages <= MAX_PAGES:
            raise EsiDataError('invalid_pages', 'ESI page count is invalid.')
        return pages

    @staticmethod
    def _decode_entries(content: bytes, character_id: int) -> list[JournalEntry]:
        try:
            payload = json.loads(content, parse_float=Decimal, parse_int=int)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise EsiDataError('invalid_data', 'ESI wallet data is invalid.') from None
        if not isinstance(payload, list):
            raise EsiDataError('invalid_data', 'ESI wallet data is invalid.')
        try:
            return [EsiClient._entry_from_payload(item, character_id) for item in payload]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise EsiDataError('invalid_data', 'ESI wallet data is invalid.') from None

    @staticmethod
    def _entry_from_payload(item: object, character_id: int) -> JournalEntry:
        if not isinstance(item, dict):
            raise TypeError
        journal_id = item['id']
        amount = item['amount']
        ref_type = item['ref_type']
        date_utc = datetime.fromisoformat(item['date']).astimezone(UTC)
        if type(journal_id) is not int or journal_id <= 0:
            raise ValueError
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise ValueError
        if not isinstance(ref_type, str) or not ref_type:
            raise ValueError
        return JournalEntry(
            journal_id=journal_id,
            character_id=character_id,
            date_utc=date_utc,
            amount=amount,
            ref_type=ref_type,
            tax=item.get('tax'),
            balance=item.get('balance'),
            description=item.get('description', ''),
            reason=item.get('reason'),
            context_id=item.get('context_id'),
            context_id_type=item.get('context_id_type'),
            first_party_id=item.get('first_party_id'),
            first_party_type=item.get('first_party_type'),
            second_party_id=item.get('second_party_id'),
            second_party_type=item.get('second_party_type'),
            tax_receiver_id=item.get('tax_receiver_id'),
        )
