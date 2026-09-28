import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import JournalEntry
from app.storage import CharacterRepository, Database, JournalRepository

UTC = timezone.utc  # noqa: UP017 - test runtime is Python 3.10.


def journal(character_id: int, journal_id: int, amount: str = "1") -> JournalEntry:
    return JournalEntry(
        journal_id=journal_id,
        character_id=character_id,
        date_utc=datetime(2026, 9, 1, 12, tzinfo=UTC),
        amount=Decimal(amount),
        ref_type="market_transaction",
        description="initial description",
    )


def test_initialize_is_idempotent_and_enables_foreign_keys(tmp_path):
    database = Database(tmp_path / "income.sqlite3")

    database.initialize()
    database.initialize()

    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO wallet_journal "
                "(character_id, journal_id, date_utc, amount, ref_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (999, 1, "x", "1", "x"),
            )


def test_upsert_authorization_creates_enabled_authorized_character(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    character = CharacterRepository(database).upsert_authorization(
        1001,
        "Astra Pilot",
        "ciphertext-one",
        datetime(2026, 9, 1, 12, tzinfo=UTC),
    )

    assert character.character_id == 1001
    assert character.name == "Astra Pilot"
    assert character.encrypted_refresh_token == "ciphertext-one"
    assert character.enabled is True
    assert character.authorization_required is False
    assert character.last_synced_at is None


def test_reauthorization_preserves_start_and_characters_are_distinct(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    repository = CharacterRepository(database)
    started_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    repository.upsert_authorization(1001, "Astra", "one", started_at)
    reauthorized = repository.upsert_authorization(
        1001, "Astra Renamed", "two", datetime(2026, 9, 2, 12, tzinfo=UTC)
    )
    second = repository.upsert_authorization(
        2002, "Borealis", "three", datetime(2026, 9, 3, 12, tzinfo=UTC)
    )

    assert reauthorized.name == "Astra Renamed"
    assert reauthorized.encrypted_refresh_token == "two"
    assert reauthorized.tracking_started_at == started_at
    assert repository.list_all() == [reauthorized, second]


def test_character_state_operations_update_only_the_requested_state(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    repository = CharacterRepository(database)
    repository.upsert_authorization(
        1001, "Astra", "one", datetime(2026, 9, 1, tzinfo=UTC)
    )

    repository.set_enabled(1001, False)
    repository.replace_refresh_token(1001, "two")
    repository.mark_authorization_required(1001)

    character = repository.list_all()[0]
    assert character.enabled is False
    assert character.encrypted_refresh_token == "two"
    assert character.authorization_required is True


def test_commit_sync_batch_preserves_decimal_and_is_idempotent(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    CharacterRepository(database).upsert_authorization(
        1001, "Astra", "token", datetime(2026, 9, 1, tzinfo=UTC)
    )
    repository = JournalRepository(database)
    entries = [journal(1001, 77, "9007199254740993.01")]

    assert (
        repository.commit_sync_batch(1001, entries, datetime(2026, 9, 2, tzinfo=UTC))
        == 1
    )
    assert (
        repository.commit_sync_batch(1001, entries, datetime(2026, 9, 3, tzinfo=UTC))
        == 0
    )
    assert repository.list_for_report(
        {1001}, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    )[0].amount == Decimal("9007199254740993.01")


def test_journal_keys_are_per_character_and_metadata_is_updated(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    characters = CharacterRepository(database)
    for character_id in (1001, 2002):
        characters.upsert_authorization(
            character_id, str(character_id), "token", datetime(2026, 9, 1, tzinfo=UTC)
        )
    repository = JournalRepository(database)
    repository.commit_sync_batch(
        1001, [journal(1001, 77)], datetime(2026, 9, 2, tzinfo=UTC)
    )
    repository.commit_sync_batch(
        1001,
        [replace(journal(1001, 77), description="updated")],
        datetime(2026, 9, 3, tzinfo=UTC),
    )
    repository.commit_sync_batch(
        2002, [journal(2002, 77)], datetime(2026, 9, 2, tzinfo=UTC)
    )

    rows = repository.list_for_report(
        {1001, 2002}, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    )
    assert [(row.character_id, row.journal_id, row.description) for row in rows] == [
        (1001, 77, "updated"),
        (2002, 77, "initial description"),
    ]


def test_failed_batch_rolls_back_rows_and_last_synced_at(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    characters = CharacterRepository(database)
    characters.upsert_authorization(
        1001, "Astra", "token", datetime(2026, 9, 1, tzinfo=UTC)
    )
    with database.connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_second BEFORE INSERT ON wallet_journal WHEN NEW.journal_id = 2 BEGIN SELECT RAISE(ABORT, 'reject'); END"
        )

    with pytest.raises(sqlite3.DatabaseError):
        JournalRepository(database).commit_sync_batch(
            1001, [journal(1001, 1), journal(1001, 2)], datetime(2026, 9, 2, tzinfo=UTC)
        )

    assert (
        JournalRepository(database).list_for_report(
            {1001}, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
        )
        == []
    )
    assert characters.list_all()[0].last_synced_at is None
