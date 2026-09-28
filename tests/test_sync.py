import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.esi import EsiAuthorizationError, EsiDataError
from app.models import JournalEntry
from app.security import TokenCipher
from app.sso import SsoError, TokenSet
from app.storage import CharacterRepository, Database, JournalRepository
from app.sync import SyncService

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
START = datetime(2026, 9, 27, 10, tzinfo=UTC)
KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def entry(
    character_id: int,
    journal_id: int,
    timestamp: datetime,
    ref_type: str = "bounty_prizes",
) -> JournalEntry:
    return JournalEntry(journal_id, character_id, timestamp, Decimal("10.00"), ref_type)


class FakeSso:
    def __init__(self, refresh_token: str = "rotated-refresh-token"):
        self.refresh_token = refresh_token
        self.calls = []

    async def refresh(self, refresh_token: str) -> TokenSet:
        self.calls.append(refresh_token)
        return TokenSet("access-token", self.refresh_token, NOW)


class FakeEsi:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def fetch_wallet_journal(self, access_token: str, character_id: int):
        self.calls.append(character_id)
        if isinstance(self.response, Exception):
            raise self.response
        return [item for item in self.response if item.character_id == character_id]


def services(tmp_path, enabled: tuple[int, ...] = (1,)):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    characters = CharacterRepository(database)
    cipher = TokenCipher(KEY)
    for character_id in (1, 2, 3, 4):
        record = characters.upsert_authorization(
            character_id,
            f"Pilot {character_id}",
            cipher.encrypt(f"refresh-{character_id}"),
            START,
        )
        if character_id not in enabled:
            characters.set_enabled(record.character_id, False)
    return characters, JournalRepository(database), cipher


def test_sync_filters_before_start_persists_rotation_and_is_idempotent(tmp_path):
    async def scenario():
        characters, journal, cipher = services(tmp_path)
        esi = FakeEsi(
            [
                entry(1, 1, datetime(2026, 9, 27, 9, 59, 59, tzinfo=UTC)),
                entry(1, 2, START),
                entry(1, 3, NOW, "unknown_type"),
            ]
        )
        service = SyncService(
            characters, journal, cipher, FakeSso(), esi, clock=lambda: NOW
        )

        first = await service.sync_character(1)
        second = await service.sync_character(1)
        stored = journal.list_for_report({1}, START, datetime(2026, 9, 29, tzinfo=UTC))
        record = characters.list_all()[0]

        assert (first.status, first.new_count, first.error_code) == ("ok", 2, None)
        assert (second.status, second.new_count) == ("ok", 0)
        assert [item.journal_id for item in stored] == [2, 3]
        assert cipher.decrypt(record.encrypted_refresh_token) == "rotated-refresh-token"
        assert record.tracking_started_at == START
        assert record.last_synced_at == NOW

    asyncio.run(scenario())


def test_failed_fetch_preserves_batch_and_sync_time_but_persists_rotated_token(
    tmp_path,
):
    async def scenario():
        characters, journal, cipher = services(tmp_path)
        journal.commit_sync_batch(1, [entry(1, 9, START)], START)
        service = SyncService(
            characters,
            journal,
            cipher,
            FakeSso(),
            FakeEsi(EsiDataError("invalid_data", "safe")),
            clock=lambda: NOW,
        )

        result = await service.sync_character(1)
        record = characters.list_all()[0]
        stored = journal.list_for_report({1}, START, datetime(2026, 9, 29, tzinfo=UTC))

        assert result.status == "failed"
        assert result.error_code == "invalid_data"
        assert [item.journal_id for item in stored] == [9]
        assert record.last_synced_at == START
        assert cipher.decrypt(record.encrypted_refresh_token) == "rotated-refresh-token"

    asyncio.run(scenario())


def test_sync_enabled_is_sequential_isolated_and_skips_disabled(tmp_path):
    async def scenario():
        characters, journal, cipher = services(tmp_path, enabled=(1, 2, 3))

        class PerCharacterEsi(FakeEsi):
            async def fetch_wallet_journal(self, access_token: str, character_id: int):
                self.calls.append(character_id)
                if character_id == 2:
                    raise EsiAuthorizationError("authorization_failed", "safe")
                return [entry(character_id, character_id, NOW)]

        esi = PerCharacterEsi([])
        results = await SyncService(
            characters, journal, cipher, FakeSso(), esi, clock=lambda: NOW
        ).sync_enabled()

        assert esi.calls == [1, 2, 3]
        assert [(item.character_id, item.status) for item in results.results] == [
            (1, "ok"),
            (2, "failed"),
            (3, "ok"),
        ]
        assert characters.list_all()[1].authorization_required is True
        assert [
            item.character_id
            for item in journal.list_for_report(
                {1, 2, 3, 4}, START, datetime(2026, 9, 29, tzinfo=UTC)
            )
        ] == [1, 3]
        assert all("access-token" not in str(item) for item in results.results)

    asyncio.run(scenario())


def test_refresh_or_decrypt_failure_marks_only_role_as_authorization_required(tmp_path):
    async def scenario():
        characters, journal, cipher = services(tmp_path, enabled=(1, 2))
        journal.commit_sync_batch(1, [entry(1, 9, START)], START)

        class FailingSso:
            async def refresh(self, refresh_token: str):
                raise SsoError("invalid_grant", "refresh-token-never-print")

        refresh_result = await SyncService(
            characters, journal, cipher, FailingSso(), FakeEsi([]), clock=lambda: NOW
        ).sync_character(1)
        decrypt_result = await SyncService(
            characters,
            journal,
            TokenCipher(Fernet.generate_key()),
            FakeSso(),
            FakeEsi([]),
            clock=lambda: NOW,
        ).sync_character(2)

        records = characters.list_all()
        assert (refresh_result.status, refresh_result.error_code) == (
            "failed",
            "authorization_failed",
        )
        assert (decrypt_result.status, decrypt_result.error_code) == (
            "failed",
            "authorization_failed",
        )
        assert records[0].authorization_required is True
        assert records[1].authorization_required is True
        assert records[0].tracking_started_at == START
        assert [
            item.journal_id
            for item in journal.list_for_report(
                {1}, START, datetime(2026, 9, 29, tzinfo=UTC)
            )
        ] == [9]
        assert "refresh-token-never-print" not in str(refresh_result)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error_code",
    ["unavailable", "request_failed", "invalid_response", "invalid_metadata"],
)
@pytest.mark.parametrize("authorization_required", [False, True])
def test_transient_sso_failure_preserves_role_authorization_and_cached_rows(
    tmp_path, error_code, authorization_required
):
    async def scenario():
        characters, journal, cipher = services(tmp_path)
        journal.commit_sync_batch(1, [entry(1, 9, START)], START)

        if authorization_required:
            characters.mark_authorization_required(1)

        class UnavailableSso:
            async def refresh(self, refresh_token: str):
                raise SsoError(error_code, "provider-detail-never-print")

        result = await SyncService(
            characters,
            journal,
            cipher,
            UnavailableSso(),
            FakeEsi([]),
            clock=lambda: NOW,
        ).sync_character(1)
        record = characters.list_all()[0]
        stored = journal.list_for_report({1}, START, datetime(2026, 9, 29, tzinfo=UTC))

        assert (result.status, result.error_code) == ("failed", "sync_failed")
        assert record.authorization_required is authorization_required
        assert record.last_synced_at == START
        assert cipher.decrypt(record.encrypted_refresh_token) == "refresh-1"
        assert [item.journal_id for item in stored] == [9]
        assert "provider-detail-never-print" not in str(result)

    asyncio.run(scenario())


def test_successful_sync_clears_stale_authorization_required_marker(tmp_path):
    async def scenario():
        characters, journal, cipher = services(tmp_path)
        characters.mark_authorization_required(1)

        result = await SyncService(
            characters,
            journal,
            cipher,
            FakeSso(),
            FakeEsi([entry(1, 8, NOW)]),
            clock=lambda: NOW,
        ).sync_character(1)

        assert (result.status, result.error_code) == ("ok", None)
        assert characters.list_all()[0].authorization_required is False

    asyncio.run(scenario())


def test_completion_clock_is_captured_after_success_or_failure_network_work(tmp_path):
    async def scenario(response, expected_status):
        characters, journal, cipher = services(tmp_path)
        events = []

        class OrderedEsi(FakeEsi):
            async def fetch_wallet_journal(self, access_token: str, character_id: int):
                events.append("fetch")
                if isinstance(response, Exception):
                    raise response
                return [entry(1, 1, NOW)]

        def clock():
            events.append("clock")
            return NOW

        result = await SyncService(
            characters, journal, cipher, FakeSso(), OrderedEsi([]), clock=clock
        ).sync_character(1)
        assert result.status == expected_status
        assert events == ["fetch", "clock"]
        assert result.completed_at == NOW
        if expected_status == "ok":
            assert characters.list_all()[0].last_synced_at == result.completed_at

    asyncio.run(scenario([], "ok"))
    asyncio.run(scenario(EsiDataError("invalid_data", "safe"), "failed"))
