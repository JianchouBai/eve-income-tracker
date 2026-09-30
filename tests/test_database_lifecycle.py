import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.database_lifecycle import DatabaseLifecycle, DatabaseLifecycleError
from app.models import JournalEntry
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    Database,
    JournalRepository,
)

UTC = timezone.utc  # noqa: UP017 - application supports Python 3.11.


def create_version_one_database(path):
    connection = sqlite3.connect(path)
    try:
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
            INSERT INTO characters VALUES (
                1001,
                'Astra',
                'private-old-token',
                0,
                0,
                '2026-09-01T12:00:00.000000+00:00',
                '2026-09-02T13:00:00.000000+00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def create_version_two_source(path):
    database = Database(path)
    database.initialize()
    groups = CharacterGroupRepository(database)
    characters = CharacterRepository(database)
    journals = JournalRepository(database)
    group = groups.create("深空 主队", datetime(2026, 9, 1, 8, tzinfo=UTC))
    character = characters.upsert_authorization(
        1001,
        "Astra",
        "private-encrypted-refresh-token",
        datetime(2026, 9, 2, 9, tzinfo=UTC),
    )
    characters.set_group(character.character_id, group.group_id)
    characters.set_enabled(character.character_id, False)
    entry = JournalEntry(
        journal_id=77,
        character_id=character.character_id,
        date_utc=datetime(2026, 9, 3, 10, tzinfo=UTC),
        amount=Decimal("9007199254740993.01"),
        ref_type="bounty_prizes",
        tax=Decimal("-7.5"),
        balance=Decimal("123.45"),
        description="private wallet description",
        reason="private reason",
        context_id=88,
        context_id_type="structure_id",
        first_party_id=1001,
        first_party_type="character_id",
        second_party_id=3003,
        second_party_type="character_id",
        tax_receiver_id=4004,
    )
    journals.commit_sync_batch(
        character.character_id,
        [entry],
        datetime(2026, 9, 4, 11, tzinfo=UTC),
    )
    return group, entry


def fixed_clock():
    return datetime(2026, 9, 30, 1, 2, 3, tzinfo=UTC)


def test_prepare_backs_up_v1_bytes_migrates_and_keeps_newest_three(tmp_path):
    path = tmp_path / "data" / "eve_income.sqlite3"
    path.parent.mkdir()
    create_version_one_database(path)
    original = path.read_bytes()
    backups = tmp_path / "backups"
    backups.mkdir()
    for stamp in (
        "20260926T000000000000Z",
        "20260927T000000000000Z",
        "20260928T000000000000Z",
        "20260929T000000000000Z",
    ):
        (backups / f"eve_income-v1-{stamp}.sqlite3").write_bytes(stamp.encode())

    DatabaseLifecycle(Database(path), backups, clock=fixed_clock).prepare()

    expected_backup = backups / "eve_income-v1-20260930T010203000000Z.sqlite3"
    assert expected_backup.read_bytes() == original
    assert Database(path).inspect_version() == 2
    assert sorted(item.name for item in backups.iterdir()) == [
        "eve_income-v1-20260928T000000000000Z.sqlite3",
        "eve_income-v1-20260929T000000000000Z.sqlite3",
        expected_backup.name,
    ]


def test_prepare_restores_original_bytes_when_migration_mutates_then_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "eve_income.sqlite3"
    create_version_one_database(path)
    original = path.read_bytes()
    database = Database(path)
    backups = tmp_path / "backups"

    def fail_after_mutation():
        path.write_bytes(b"private-mutated-database")
        raise RuntimeError("private-migration-details")

    monkeypatch.setattr(database, "initialize", fail_after_mutation)

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(database, backups, clock=fixed_clock).prepare()

    assert caught.value.code == "migration_failed"
    assert str(caught.value) == "migration_failed"
    assert "private-" not in repr(caught.value)
    assert path.read_bytes() == original
    assert [item.read_bytes() for item in backups.iterdir()] == [original]


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("corrupt", "database_corrupt"),
        ("future", "database_future"),
    ],
)
def test_prepare_rejects_corrupt_or_future_database_without_mutation_or_backup(
    tmp_path, kind, expected_code
):
    path = tmp_path / "eve_income.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"private-not-a-sqlite-database")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('private-keep-me')")
            connection.execute("PRAGMA user_version = 3")
    original = path.read_bytes()
    backups = tmp_path / "backups"

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(Database(path), backups, clock=fixed_clock).prepare()

    assert caught.value.code == expected_code
    assert path.read_bytes() == original
    assert not backups.exists()
    assert "private-" not in f"{caught.value!s} {caught.value!r}"


def test_prepare_rejects_locked_database_without_mutation_or_backup(tmp_path):
    path = tmp_path / "eve_income.sqlite3"
    create_version_one_database(path)
    original = path.read_bytes()
    backups = tmp_path / "backups"
    lock = sqlite3.connect(path, timeout=0)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DatabaseLifecycleError) as caught:
            DatabaseLifecycle(Database(path), backups, clock=fixed_clock).prepare()
    finally:
        lock.rollback()
        lock.close()

    assert caught.value.code == "database_locked"
    assert path.read_bytes() == original
    assert not backups.exists()


def test_prepare_does_not_migrate_when_backup_cannot_be_written(tmp_path, monkeypatch):
    import app.database_lifecycle as lifecycle_module

    path = tmp_path / "eve_income.sqlite3"
    create_version_one_database(path)
    original = path.read_bytes()
    backups = tmp_path / "backups"

    def denied(*args, **kwargs):
        raise PermissionError("private-backup-path")

    monkeypatch.setattr(lifecycle_module.tempfile, "mkstemp", denied)

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(Database(path), backups, clock=fixed_clock).prepare()

    assert caught.value.code == "backup_unwritable"
    assert path.read_bytes() == original
    assert not list(backups.glob("*"))
    assert "private-" not in str(caught.value)


def test_prepare_creates_new_database_atomically_and_leaves_current_v2_untouched(
    tmp_path,
):
    new_path = tmp_path / "new" / "eve_income.sqlite3"
    new_lifecycle = DatabaseLifecycle(
        Database(new_path),
        tmp_path / "new-backups",
        clock=fixed_clock,
    )

    new_lifecycle.prepare()

    assert Database(new_path).inspect_version() == 2
    assert list(new_path.parent.glob("*.tmp")) == []
    current_bytes = new_path.read_bytes()

    new_lifecycle.prepare()

    assert new_path.read_bytes() == current_bytes
    assert not (tmp_path / "new-backups").exists()


def test_import_v2_preserves_history_and_structure_but_requires_reauthorization(
    tmp_path,
):
    source = tmp_path / "legacy" / "eve_income.sqlite3"
    source.parent.mkdir()
    expected_group, expected_entry = create_version_two_source(source)
    source_bytes = source.read_bytes()
    target = tmp_path / "desktop" / "eve_income.sqlite3"
    lifecycle = DatabaseLifecycle(
        Database(target),
        tmp_path / "backups",
        clock=fixed_clock,
    )

    result = lifecycle.import_legacy(source)

    assert result.source_version == 2
    assert result.characters_imported == 1
    assert result.journal_entries_imported == 1
    assert source.read_bytes() == source_bytes
    assert Database(target).inspect_version() == 2
    groups = CharacterGroupRepository(Database(target)).list_all()
    assert groups == [expected_group]
    character = CharacterRepository(Database(target)).list_all()[0]
    assert character.character_id == 1001
    assert character.name == "Astra"
    assert character.enabled is False
    assert character.authorization_required is True
    assert character.encrypted_refresh_token == ""
    assert character.tracking_started_at == datetime(2026, 9, 2, 9, tzinfo=UTC)
    assert character.last_synced_at == datetime(2026, 9, 4, 11, tzinfo=UTC)
    assert character.group_id == expected_group.group_id
    assert JournalRepository(Database(target)).list_for_report(
        {1001},
        datetime(2026, 9, 3, tzinfo=UTC),
        datetime(2026, 9, 4, tzinfo=UTC),
    ) == [expected_entry]


def test_import_refuses_existing_target_without_touching_either_database(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "desktop.sqlite3"
    create_version_two_source(source)
    Database(target).initialize()
    source_bytes = source.read_bytes()
    target_bytes = target.read_bytes()

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(Database(target), tmp_path / "backups").import_legacy(source)

    assert caught.value.code == "import_target_exists"
    assert source.read_bytes() == source_bytes
    assert target.read_bytes() == target_bytes


def test_import_refuses_same_source_and_target_without_mutation(tmp_path):
    source = tmp_path / "eve_income.sqlite3"
    create_version_two_source(source)
    source_bytes = source.read_bytes()

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(Database(source), tmp_path / "backups").import_legacy(source)

    assert caught.value.code == "import_same_database"
    assert source.read_bytes() == source_bytes


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("not_sqlite", "import_invalid"),
        ("missing_schema", "import_invalid"),
        ("future", "import_future"),
    ],
)
def test_import_refuses_invalid_missing_or_future_source_without_partial_target(
    tmp_path, kind, expected_code
):
    source = tmp_path / f"{kind}.sqlite3"
    if kind == "not_sqlite":
        source.write_bytes(b"private-not-sqlite")
    else:
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('private-source')")
            connection.execute(f"PRAGMA user_version = {3 if kind == 'future' else 2}")
    source_bytes = source.read_bytes()
    target = tmp_path / "target" / "eve_income.sqlite3"

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(Database(target), tmp_path / "backups").import_legacy(source)

    assert caught.value.code == expected_code
    assert source.read_bytes() == source_bytes
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))
    assert "private-" not in f"{caught.value!s} {caught.value!r}"


def test_failed_import_changes_neither_source_nor_target(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    create_version_two_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TRIGGER reject_credential_clear "
            "BEFORE UPDATE OF encrypted_refresh_token ON characters "
            "BEGIN SELECT RAISE(ABORT, 'private-reject'); END"
        )
    source_bytes = source.read_bytes()
    target = tmp_path / "target" / "eve_income.sqlite3"

    with pytest.raises(DatabaseLifecycleError) as caught:
        DatabaseLifecycle(Database(target), tmp_path / "backups").import_legacy(source)

    assert caught.value.code == "import_failed"
    assert source.read_bytes() == source_bytes
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))
    assert "private-" not in f"{caught.value!s} {caught.value!r}"
