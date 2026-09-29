import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import storage
from app.models import JournalEntry
from app.storage import CharacterRepository, Database, JournalRepository

UTC = timezone.utc  # noqa: UP017 - test runtime is Python 3.10.

INVALID_UTC_BOUNDARIES = (
    datetime(2026, 9, 1, 12),  # noqa: DTZ001 - deliberate naive boundary input.
    datetime(2026, 9, 1, 12, tzinfo=timezone(timedelta(hours=8))),
)


def journal(character_id: int, journal_id: int, amount: str = "1") -> JournalEntry:
    return JournalEntry(
        journal_id=journal_id,
        character_id=character_id,
        date_utc=datetime(2026, 9, 1, 12, tzinfo=UTC),
        amount=Decimal(amount),
        ref_type="market_transaction",
        description="initial description",
    )


def create_version_one_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE characters (
                character_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                encrypted_refresh_token TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                authorization_required INTEGER NOT NULL,
                tracking_started_at TEXT NOT NULL,
                last_synced_at TEXT
            );
            CREATE TABLE wallet_journal (
                character_id INTEGER NOT NULL,
                journal_id INTEGER NOT NULL,
                date_utc TEXT NOT NULL,
                amount TEXT NOT NULL,
                ref_type TEXT NOT NULL,
                tax TEXT,
                balance TEXT,
                description TEXT NOT NULL,
                reason TEXT,
                context_id INTEGER,
                context_id_type TEXT,
                first_party_id INTEGER,
                first_party_type TEXT,
                second_party_id INTEGER,
                second_party_type TEXT,
                tax_receiver_id INTEGER,
                PRIMARY KEY (character_id, journal_id),
                FOREIGN KEY (character_id) REFERENCES characters(character_id)
            );
            CREATE INDEX wallet_journal_character_date_idx
                ON wallet_journal(character_id, date_utc);
            """
        )
        connection.execute(
            "INSERT INTO characters VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                1001,
                "Astra",
                "encrypted-token",
                0,
                1,
                "2026-09-01T12:00:00.000000+00:00",
                "2026-09-02T13:00:00.000000+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO wallet_journal VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1001,
                77,
                "2026-09-02T12:00:00.000000+00:00",
                "9007199254740993.01",
                "bounty_prizes",
                "-7.5",
                "123.45",
                "private description",
                "private reason",
                88,
                "structure_id",
                1001,
                "character_id",
                3003,
                "character_id",
                4004,
            ),
        )


def test_initialize_creates_version_two_group_schema(tmp_path):
    database = Database(tmp_path / "income.sqlite3")

    database.initialize()

    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "character_groups" in tables
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(characters)")
        }
        assert columns["group_id"][3] == 0
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(characters)")
        }
        assert "characters_group_id_idx" in indexes


def test_initialize_migrates_version_one_without_data_loss(tmp_path):
    path = tmp_path / "income.sqlite3"
    create_version_one_database(path)
    database = Database(path)

    database.initialize()

    character = CharacterRepository(database).list_all()[0]
    assert character.character_id == 1001
    assert character.name == "Astra"
    assert character.encrypted_refresh_token == "encrypted-token"
    assert character.enabled is False
    assert character.authorization_required is True
    assert character.tracking_started_at == datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert character.last_synced_at == datetime(2026, 9, 2, 13, tzinfo=UTC)
    assert character.group_id is None
    assert JournalRepository(database).list_for_report(
        {1001},
        datetime(2026, 9, 2, tzinfo=UTC),
        datetime(2026, 9, 3, tzinfo=UTC),
    ) == [
        JournalEntry(
            journal_id=77,
            character_id=1001,
            date_utc=datetime(2026, 9, 2, 12, tzinfo=UTC),
            amount=Decimal("9007199254740993.01"),
            ref_type="bounty_prizes",
            tax=Decimal("-7.5"),
            balance=Decimal("123.45"),
            description="private description",
            reason="private reason",
            context_id=88,
            context_id_type="structure_id",
            first_party_id=1001,
            first_party_type="character_id",
            second_party_id=3003,
            second_party_type="character_id",
            tax_receiver_id=4004,
        )
    ]


def test_initialize_rejects_future_schema_without_mutation(tmp_path):
    path = tmp_path / "income.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('keep-me')")
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(RuntimeError, match="newer database schema"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT value FROM sentinel").fetchone()[0] == "keep-me"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'characters'"
            ).fetchone()[0]
            == 0
        )


def test_version_one_migration_failure_rolls_back_schema_changes(tmp_path):
    path = tmp_path / "income.sqlite3"
    create_version_one_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE characters ADD COLUMN group_id INTEGER")

    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT name FROM characters").fetchone()[0] == "Astra"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'character_groups'"
            ).fetchone()[0]
            == 0
        )


def test_initialize_is_idempotent_and_enables_foreign_keys(tmp_path):
    database = Database(tmp_path / "income.sqlite3")

    database.initialize()
    database.initialize()

    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO wallet_journal "
                "(character_id, journal_id, date_utc, amount, ref_type, description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (999, 1, "x", "1", "x", "orphan"),
            )


def test_group_repository_normalizes_orders_renames_and_validates_names(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    groups = storage.CharacterGroupRepository(database)
    created_at = datetime(2026, 9, 1, 12, tzinfo=UTC)

    alpha = groups.create(" Alpha ", created_at)
    chinese = groups.create("采矿组", datetime(2026, 9, 2, 12, tzinfo=UTC))

    assert alpha.name == "Alpha"
    assert alpha.created_at == created_at
    assert groups.list_all() == [alpha, chinese]
    assert groups.get(alpha.group_id) == alpha
    assert groups.get(999) is None
    renamed = groups.rename(alpha.group_id, "  PVE 主队  ")
    assert renamed.name == "PVE 主队"
    assert renamed.created_at == created_at
    with pytest.raises(storage.GroupNameConflictError):
        groups.create("pve 主队", datetime(2026, 9, 3, tzinfo=UTC))
    for invalid_name in ("   ", "x" * 51):
        with pytest.raises(ValueError, match="1 to 50"):
            groups.create(invalid_name, created_at)
    with pytest.raises(storage.UnknownGroupError):
        groups.rename(999, "missing")
    with pytest.raises(storage.UnknownGroupError):
        groups.delete(999)


@pytest.mark.parametrize("created_at", INVALID_UTC_BOUNDARIES)
def test_group_creation_rejects_non_utc_timestamp(tmp_path, created_at):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        storage.CharacterGroupRepository(database).create("Alpha", created_at)


def test_character_membership_reassigns_and_returns_to_ungrouped(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    characters = CharacterRepository(database)
    groups = storage.CharacterGroupRepository(database)
    character = characters.upsert_authorization(
        1001, "Astra", "token", datetime(2026, 9, 1, tzinfo=UTC)
    )
    first = groups.create("First", datetime(2026, 9, 1, tzinfo=UTC))
    second = groups.create("Second", datetime(2026, 9, 2, tzinfo=UTC))

    assert character.group_id is None
    assert characters.list_for_group(None) == [character]
    assigned = characters.set_group(1001, first.group_id)
    assert assigned.group_id == first.group_id
    reassigned = characters.set_group(1001, second.group_id)
    assert reassigned.group_id == second.group_id
    assert characters.list_for_group(first.group_id) == []
    assert characters.list_for_group(second.group_id) == [reassigned]
    ungrouped = characters.set_group(1001, None)
    assert ungrouped.group_id is None
    assert characters.list_for_group(None) == [ungrouped]
    with pytest.raises(storage.UnknownCharacterError):
        characters.set_group(999, first.group_id)
    with pytest.raises(storage.UnknownGroupError):
        characters.set_group(1001, 999)


def test_reauthorization_preserves_group_assignment(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    characters = CharacterRepository(database)
    group = storage.CharacterGroupRepository(database).create(
        "Alpha", datetime(2026, 9, 1, tzinfo=UTC)
    )
    characters.upsert_authorization(
        1001, "Astra", "one", datetime(2026, 9, 1, tzinfo=UTC)
    )
    characters.set_group(1001, group.group_id)

    reauthorized = characters.upsert_authorization(
        1001, "Astra Prime", "two", datetime(2026, 9, 2, tzinfo=UTC)
    )

    assert reauthorized.group_id == group.group_id


def test_delete_group_ungroups_members_and_preserves_wallet_history(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    characters = CharacterRepository(database)
    groups = storage.CharacterGroupRepository(database)
    group = groups.create("Alpha", datetime(2026, 9, 1, tzinfo=UTC))
    for character_id in (1001, 2002):
        characters.upsert_authorization(
            character_id,
            str(character_id),
            f"token-{character_id}",
            datetime(2026, 9, 1, tzinfo=UTC),
        )
        characters.set_group(character_id, group.group_id)
    JournalRepository(database).commit_sync_batch(
        1001, [journal(1001, 77)], datetime(2026, 9, 2, tzinfo=UTC)
    )

    assert groups.delete(group.group_id) == (1001, 2002)

    assert groups.get(group.group_id) is None
    assert [item.group_id for item in characters.list_all()] == [None, None]
    assert characters.list_all()[0].encrypted_refresh_token == "token-1001"
    assert JournalRepository(database).list_for_report(
        {1001},
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 2, tzinfo=UTC),
    ) == [journal(1001, 77)]


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


@pytest.mark.parametrize("authorized_at", INVALID_UTC_BOUNDARIES)
def test_authorization_rejects_non_utc_timestamp(tmp_path, authorized_at):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        CharacterRepository(database).upsert_authorization(
            1001, "Astra", "token", authorized_at
        )


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


@pytest.mark.parametrize("completed_at", INVALID_UTC_BOUNDARIES)
def test_sync_completion_rejects_non_utc_timestamp(tmp_path, completed_at):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    CharacterRepository(database).upsert_authorization(
        1001, "Astra", "token", datetime(2026, 9, 1, tzinfo=UTC)
    )

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        JournalRepository(database).commit_sync_batch(
            1001, [journal(1001, 77)], completed_at
        )


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


def test_report_range_rejects_non_utc_boundaries(tmp_path):
    repository = JournalRepository(Database(tmp_path / "income.sqlite3"))
    valid = datetime(2026, 9, 2, tzinfo=UTC)

    for invalid in INVALID_UTC_BOUNDARIES:
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            repository.list_for_report({1001}, invalid, valid)
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            repository.list_for_report({1001}, valid, invalid)


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
