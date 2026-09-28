from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.esi import EsiAuthorizationError, EsiError
from app.security import TokenCipher
from app.storage import CharacterRepository, JournalRepository


@dataclass(frozen=True, slots=True)
class SyncResult:
    character_id: int
    new_count: int
    completed_at: datetime
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class SyncAllResult:
    results: tuple[SyncResult, ...]


class SyncService:
    def __init__(
        self,
        characters: CharacterRepository,
        journal: JournalRepository,
        cipher: TokenCipher,
        sso: object,
        esi: object,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._characters = characters
        self._journal = journal
        self._cipher = cipher
        self._sso = sso
        self._esi = esi
        self._clock = clock

    async def sync_character(
        self, character_id: int, initial_access_token: str | None = None
    ) -> SyncResult:
        record = next(
            (item for item in self._characters.list_all() if item.character_id == character_id),
            None,
        )
        completed_at = self._clock()
        if record is None:
            return SyncResult(character_id, 0, completed_at, 'failed', 'unknown_character')
        try:
            access_token = initial_access_token
            if access_token is None:
                tokens = await self._sso.refresh(
                    self._cipher.decrypt(record.encrypted_refresh_token)
                )
                access_token = tokens.access_token
                if tokens.refresh_token != self._cipher.decrypt(record.encrypted_refresh_token):
                    self._characters.replace_refresh_token(
                        character_id, self._cipher.encrypt(tokens.refresh_token)
                    )
            entries = await self._esi.fetch_wallet_journal(access_token, character_id)
            retained = [item for item in entries if item.date_utc >= record.tracking_started_at]
            new_count = self._journal.commit_sync_batch(character_id, retained, completed_at)
            return SyncResult(character_id, new_count, completed_at, 'ok', None)
        except EsiAuthorizationError:
            self._characters.mark_authorization_required(character_id)
            return SyncResult(character_id, 0, completed_at, 'failed', 'authorization_failed')
        except EsiError as error:
            return SyncResult(character_id, 0, completed_at, 'failed', error.code)
        except Exception:  # noqa: BLE001 - sync is a sanitized service boundary.
            return SyncResult(character_id, 0, completed_at, 'failed', 'sync_failed')

    async def sync_enabled(self) -> SyncAllResult:
        results = []
        for record in self._characters.list_all():
            if record.enabled:
                results.append(await self.sync_character(record.character_id))
        return SyncAllResult(tuple(results))
