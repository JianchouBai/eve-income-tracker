"""Pre-writer migration recovery with a real location controller."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from app.data_location_controller import DataLocationController, DataLocationError
from app.data_location_store import (
    MemoryDataLocationStore,
    MigrationTransaction,
    PendingMigration,
)
from app.data_roots import DataRootMarker, DataRootReference, write_data_root_marker
from app.paths import resolve_app_paths


def ready_source(tmp_path):
    paths = resolve_app_paths(
        frozen=True,
        bundle_root=tmp_path / "program",
        data_root=tmp_path / "source" / "EVE Income Tracker",
    )
    paths.data_dir.mkdir(parents=True)
    paths.config_dir.mkdir()
    paths.key_path.write_bytes(b"protected-key")
    with closing(sqlite3.connect(paths.database_path)) as db:
        db.execute("CREATE TABLE records(value INTEGER)")
        db.execute("INSERT INTO records VALUES(42)")
        db.commit()
    reference = DataRootReference(paths.data_root, uuid4().hex, 1)
    write_data_root_marker(
        paths.data_root,
        DataRootMarker(reference, "2026-10-02T00:00:00Z", "ready"),
    )
    store = MemoryDataLocationStore()
    store.begin_current_switch(reference)
    store.finalize_current_switch()
    return paths, reference, store


def controller_for(tmp_path, store):
    def validate(paths):
        assert paths.key_path.read_bytes() == b"protected-key"
        with closing(
            sqlite3.connect(paths.database_path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            assert db.execute("SELECT value FROM records").fetchall() == [(42,)]

    return DataLocationController(
        store,
        resource_root=tmp_path / "program",
        default_parent=tmp_path / "local",
        existing_validator=validate,
    )


@pytest.mark.parametrize("failure", ["occupied", "space", "copy"])
def test_failed_request_can_cancel_and_schedule_another_target(
    tmp_path, monkeypatch, failure
):
    paths, source, store = ready_source(tmp_path)
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    controller = controller_for(tmp_path, store)
    controller.schedule_migration(first, create_recovery=False)
    if failure == "occupied":
        target = first / "EVE Income Tracker"
        target.mkdir()
        (target / "foreign").write_bytes(b"keep")
    elif failure == "space":
        import app.data_migration as migration

        real_usage = migration.shutil.disk_usage
        monkeypatch.setattr(
            migration.shutil,
            "disk_usage",
            lambda path: type(real_usage(path))(1, 1, 0),
        )
    else:

        def fail_copy(*args):
            raise OSError("private copy failure")

        monkeypatch.setattr(controller._migration, "_copy", fail_copy)
    with pytest.raises(DataLocationError) as caught:
        controller.execute_pending_migration()
    info = controller.migration_failure_info(caught.value.code)
    assert info.source == source.root
    assert info.target == first / "EVE Income Tracker"
    assert info.can_cancel is True
    assert "private" not in repr(info)
    assert controller.cancel_pending_migration() == source
    assert store.read_pending_migration() is None
    assert store.read_current() == source
    assert paths.database_path.is_file()
    monkeypatch.undo()
    if failure == "occupied":
        assert (first / "EVE Income Tracker" / "foreign").read_bytes() == b"keep"
    assert controller.schedule_migration(second, create_recovery=False).target_root == (
        second / "EVE Income Tracker"
    )


def test_cancel_preserves_unresolved_transaction_and_blocks_new_migration(tmp_path):
    _, source, store = ready_source(tmp_path)
    first = tmp_path / "first"
    first.mkdir()
    controller = controller_for(tmp_path, store)
    controller.schedule_migration(first, create_recovery=False)
    transaction = MigrationTransaction(
        1,
        uuid4().hex,
        source,
        DataRootReference(first / "EVE Income Tracker", uuid4().hex, 1),
        "2026-10-02T00:00:00Z",
        None,
    )
    store.write_migration_transaction(transaction)
    assert controller.cancel_pending_migration() == source
    assert store.read_migration_transaction() == transaction
    assert controller.status().migration_recovery_pending is True
    with pytest.raises(DataLocationError, match="migration_recovery_pending"):
        controller.schedule_migration(tmp_path / "other", create_recovery=False)


def test_cancelled_request_can_reconcile_then_schedule_another_target(tmp_path):
    _, source, store = ready_source(tmp_path)
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    controller = controller_for(tmp_path, store)
    controller.schedule_migration(first, create_recovery=False)
    store.write_migration_transaction(
        MigrationTransaction(
            1,
            uuid4().hex,
            source,
            DataRootReference(first / "EVE Income Tracker", uuid4().hex, 1),
            "2026-10-02T00:00:00Z",
            None,
        )
    )

    assert controller.cancel_pending_migration() == source
    assert controller.execute_pending_migration() is None
    assert store.read_migration_transaction() is None
    assert controller.schedule_migration(second, create_recovery=False).target_root == (
        second / "EVE Income Tracker"
    )


@pytest.mark.parametrize("damage", ["key", "marker", "registry", "committed"])
def test_cancel_fails_closed_if_source_or_selection_is_not_authoritative(
    tmp_path, damage
):
    paths, source, store = ready_source(tmp_path)
    target_parent = tmp_path / "target"
    target_parent.mkdir()
    request = PendingMigration(target_parent, False)
    store.write_pending_migration(request)
    if damage == "key":
        paths.key_path.write_bytes(b"invalid")
    elif damage == "marker":
        (source.root / ".eve-income-tracker-data.json").unlink()
    elif damage == "registry":
        store.backend.values["DataRootId"] = (uuid4().hex, 1)
    else:
        store.write_migration_transaction(
            MigrationTransaction(
                1,
                uuid4().hex,
                source,
                DataRootReference(target_parent / "EVE Income Tracker", uuid4().hex, 1),
                "2026-10-02T00:00:00Z",
                None,
            )
        )
        target_paths = resolve_app_paths(
            frozen=True,
            bundle_root=tmp_path / "program",
            data_root=target_parent / "EVE Income Tracker",
        )
        target_paths.data_dir.mkdir(parents=True)
        target_paths.config_dir.mkdir()
        target_paths.key_path.write_bytes(b"protected-key")
        with closing(sqlite3.connect(target_paths.database_path)) as db:
            db.execute("CREATE TABLE records(value INTEGER)")
            db.execute("INSERT INTO records VALUES(42)")
            db.commit()
        selected = store.read_migration_transaction().target
        write_data_root_marker(
            selected.root, DataRootMarker(selected, "2026-10-02T00:00:00Z", "ready")
        )
        store.begin_current_switch(selected)
        store.finalize_current_switch()
    controller = controller_for(tmp_path, store)
    assert controller.migration_failure_info("copy_failed").can_cancel is False
    with pytest.raises(DataLocationError):
        controller.cancel_pending_migration()
    assert store.read_pending_migration() == request
