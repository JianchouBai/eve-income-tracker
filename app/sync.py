from asyncio import Lock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken

from app.esi import EsiAuthorizationError, EsiError
from app.lp_models import LpSyncResult
from app.lp_sync import LpSyncService
from app.security import TokenCipher
from app.sso import CharacterIdentity, SsoError, TokenSet
from app.storage import CharacterRepository, JournalRepository


@dataclass(frozen=True, slots=True)
class SyncResult:
    character_id: int
    new_count: int
    completed_at: datetime
    status: str
    error_code: str | None
    lp: LpSyncResult | None = None


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
        *,
        lp: LpSyncService | None = None,
    ):
        self._characters = characters
        self._journal = journal
        self._cipher = cipher
        self._sso = sso
        self._esi = esi
        self._clock = clock
        self._lp = lp
        self._locks: dict[int, Lock] = {}

    def _lock(self, character_id: int) -> Lock:
        return self._locks.setdefault(character_id, Lock())

    async def save_authorization_and_sync(
        self, identity: CharacterIdentity, tokens: TokenSet, authorized_at: datetime
    ) -> SyncResult:
        async with self._lock(identity.character_id):
            self._characters.upsert_authorization(
                identity.character_id,
                identity.character_name,
                self._cipher.encrypt(tokens.refresh_token),
                authorized_at,
                granted_scopes=identity.scopes,
            )
            return await self._sync_locked(
                identity.character_id, tokens.access_token, identity
            )

    async def sync_character(
        self, character_id: int, initial_access_token: str | None = None
    ) -> SyncResult:
        async with self._lock(character_id):
            return await self._sync_locked(character_id, initial_access_token)

    async def _sync_locked(
        self, character_id, initial_access_token=None, identity=None
    ):
        record = next(
            (x for x in self._characters.list_all() if x.character_id == character_id),
            None,
        )
        if record is None:
            return SyncResult(
                character_id, 0, self._clock(), "failed", "unknown_character"
            )
        try:
            access_token = initial_access_token
            if access_token is None:
                old_refresh_token = self._cipher.decrypt(record.encrypted_refresh_token)
                tokens = await self._sso.refresh(old_refresh_token)
                access_token = tokens.access_token
                identity = await self._sso.verify_access_token(access_token)
                if identity.character_id != character_id:
                    raise SsoError("invalid_subject_claim", "Token identity mismatch.")
                if tokens.refresh_token != old_refresh_token:
                    self._characters.replace_refresh_token(
                        character_id, self._cipher.encrypt(tokens.refresh_token)
                    )
            elif identity is None:
                identity = await self._sso.verify_access_token(access_token)
            if identity.character_id != character_id:
                raise SsoError("invalid_subject_claim", "Token identity mismatch.")
            self._characters.replace_granted_scopes(character_id, identity.scopes)
            self._characters.clear_authorization_required(character_id)
        except (InvalidToken, SsoError) as error:
            required = isinstance(error, InvalidToken) or error.code not in {
                "unavailable",
                "request_failed",
                "invalid_response",
                "invalid_metadata",
            }
            if required:
                self._characters.mark_authorization_required(character_id)
            at = self._clock()
            lp_result = None
            if self._lp is not None:
                code = "authorization_failed" if required else "lp_sync_failed"
                self._lp.repository.record_problem(character_id, code, at)
                lp_result = LpSyncResult(
                    character_id,
                    "authorization_required" if required else "failed",
                    at,
                    error_code=code,
                )
            return SyncResult(
                character_id,
                0,
                at,
                "failed",
                "authorization_failed" if required else "sync_failed",
                lp_result,
            )
        except Exception:  # noqa: BLE001 - credential boundary remains sanitized.
            return SyncResult(character_id, 0, self._clock(), "failed", "sync_failed")
        new_count, status, error_code = 0, "ok", None
        wallet_auth_failed = False
        try:
            entries = await self._esi.fetch_wallet_journal(access_token, character_id)
            retained = [
                item for item in entries if item.date_utc >= record.tracking_started_at
            ]
            completed_at = self._clock()
            new_count = self._journal.commit_sync_batch(
                character_id, retained, completed_at
            )
        except EsiAuthorizationError:
            self._characters.mark_authorization_required(character_id)
            status, error_code, wallet_auth_failed = (
                "failed",
                "authorization_failed",
                True,
            )
            completed_at = self._clock()
        except EsiError as error:
            status, error_code, completed_at = "failed", error.code, self._clock()
        except Exception:  # noqa: BLE001 - wallet failure must not discard LP data.
            status, error_code, completed_at = "failed", "sync_failed", self._clock()
        lp_result = None
        if self._lp is not None:
            if wallet_auth_failed:
                self._lp.repository.record_problem(
                    character_id, "authorization_failed", completed_at
                )
                lp_result = LpSyncResult(
                    character_id,
                    "authorization_required",
                    completed_at,
                    error_code="authorization_failed",
                )
            else:
                try:
                    lp_result = await self._lp.sync_character(
                        character_id, access_token, identity.scopes
                    )
                except EsiAuthorizationError:
                    self._characters.mark_authorization_required(character_id)
                    at = self._clock()
                    self._lp.repository.record_problem(
                        character_id, "authorization_failed", at
                    )
                    lp_result = LpSyncResult(
                        character_id,
                        "authorization_required",
                        at,
                        error_code="authorization_failed",
                    )
                except Exception:  # noqa: BLE001 - retain committed wallet results.
                    at = self._clock()
                    self._lp.repository.record_problem(
                        character_id, "lp_sync_failed", at
                    )
                    lp_result = LpSyncResult(
                        character_id, "failed", at, error_code="lp_sync_failed"
                    )
        return SyncResult(
            character_id, new_count, completed_at, status, error_code, lp_result
        )

    async def sync_group(self, group_id: int) -> SyncAllResult:
        results = []
        for record in self._characters.list_for_group(group_id):
            if record.enabled:
                results.append(await self.sync_character(record.character_id))
        return SyncAllResult(tuple(results))
