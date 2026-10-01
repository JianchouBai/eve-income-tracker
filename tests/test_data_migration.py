"""Transactional migration behavior and injected failures."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest

from app import data_migration
from app.data_location_store import MemoryDataLocationStore, PendingCleanup
from app.data_migration import DataMigrationError, DataMigrationService
from app.data_roots import (
    MARKER_NAME,
    DataRootMarker,
    DataRootReference,
    read_data_root_marker,
    validate_existing_data_root,
    write_data_root_marker,
)
from app.migration_recovery import ARCHIVE_NAME, RecoverySnapshotService
from app.paths import resolve_app_paths


def make_source(tmp_path):
    paths = resolve_app_paths(
        frozen=True,
        bundle_root=tmp_path / "program",
        data_root=tmp_path / "volume-alpha" / "EVE Income Tracker",
    )
    paths.data_dir.mkdir(parents=True)
    paths.config_dir.mkdir()
    paths.logs_dir.mkdir()
    paths.backups_dir.mkdir()
    paths.webview_storage_dir.mkdir()
    paths.key_path.write_bytes(b"private protected key")
    (paths.logs_dir / "app.log").write_bytes(b"log")
    (paths.backups_dir / "ordinary.zip").write_bytes(b"backup")
    (paths.backups_dir / ARCHIVE_NAME).write_bytes(b"old recovery must not move")
    (paths.webview_storage_dir / "cache").write_bytes(b"cache")
    (paths.config_dir / "settings.json").write_bytes(b'{"setting":42}')
    (paths.data_root / "extra").mkdir()
    (paths.data_root / "extra" / "empty").mkdir()
    with closing(sqlite3.connect(paths.database_path)) as db:
        db.execute("CREATE TABLE income (amount INTEGER)")
        db.execute("INSERT INTO income VALUES (42)")
        db.commit()
    reference = DataRootReference(paths.data_root, uuid4().hex, 1)
    write_data_root_marker(
        paths.data_root, DataRootMarker(reference, "2026-10-02T00:00:00Z", "ready")
    )
    store = MemoryDataLocationStore()
    store.begin_current_switch(reference)
    store.finalize_current_switch()
    target_parent = tmp_path / "volume-beta"
    target_parent.mkdir()
    return paths, reference, store, target_parent


def service_for(paths, store, validator=None):
    def validate(destination):
        assert destination.key_path.read_bytes() == b"private protected key"
        with closing(
            sqlite3.connect(destination.database_path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            assert db.execute("SELECT amount FROM income").fetchall()

    return DataMigrationService(
        store,
        resource_root=paths.resource_root,
        destination_validator=validator or validate,
    )


def source_bytes(paths):
    return {
        item.relative_to(paths.data_root): item.read_bytes()
        for item in paths.data_root.rglob("*")
        if item.is_file()
    }


def fail(*args, **kwargs):
    raise OSError("secret key and private path must not leak")


def test_estimate_reports_copy_recovery_and_free_bytes_without_writes(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    before = source_bytes(paths)
    monkeypatch.setattr(
        data_migration.shutil, "disk_usage", lambda _: SimpleNamespace(free=987654321)
    )
    service = service_for(paths, store)
    estimate = service.estimate(reference, parent, create_recovery=True)
    without = service.estimate(reference, parent, create_recovery=False)
    copied = sum(
        len(value)
        for name, value in before.items()
        if name not in (Path(MARKER_NAME), Path("backups") / ARCHIVE_NAME)
    )
    assert estimate.target_root == parent / "EVE Income Tracker"
    assert estimate.copy_bytes == copied
    assert estimate.recovery_bytes >= paths.database_path.stat().st_size + 21
    assert estimate.free_bytes == 987654321
    assert without.copy_bytes == copied
    assert without.recovery_bytes == 0
    assert source_bytes(paths) == before
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("create_recovery", [False, True])
def test_success_moves_all_data_and_can_migrate_again(tmp_path, create_recovery):
    paths, reference, store, parent = make_source(tmp_path)
    original = source_bytes(paths)
    service = service_for(paths, store)
    result = service.migrate(reference, parent, create_recovery=create_recovery)
    assert result.original_removed is True
    assert result.pending_cleanup is None
    assert not reference.root.exists()
    assert reference.root.parent.exists()
    assert store.read_current() == result.reference
    assert result.reference.root_id != reference.root_id
    assert store.read_pending_cleanup() is None
    destination = validate_existing_data_root(
        result.reference, resource_root=paths.resource_root
    )
    for name, value in original.items():
        if name in (
            Path(MARKER_NAME),
            Path("backups") / ARCHIVE_NAME,
            Path("data/eve_income.sqlite3"),
        ):
            continue
        assert (destination.data_root / name).read_bytes() == value
    assert (destination.data_root / "extra" / "empty").is_dir()
    archive = destination.backups_dir / ARCHIVE_NAME
    assert archive.exists() is create_recovery
    if create_recovery:
        with ZipFile(archive) as snapshot:
            assert snapshot.read("config/local.key") == b"private protected key"
    next_parent = tmp_path / "volume-gamma"
    next_parent.mkdir()
    again = service.migrate(result.reference, next_parent, create_recovery=False)
    assert again.original_removed
    assert store.read_current() == again.reference
    assert not result.reference.root.exists()
    assert not (again.reference.root / "backups" / ARCHIVE_NAME).exists()


def test_staging_marker_validation_rename_and_registry_order(tmp_path, monkeypatch):
    paths, reference, store, parent = make_source(tmp_path)
    events = []
    real_write = data_migration.write_staged_data_root_marker
    real_rename = data_migration.os.rename
    real_begin = store.begin_current_switch
    real_finalize = store.finalize_current_switch

    def marker(root, value):
        events.append(("marker", root, value.state))
        real_write(root, value)

    def rename(source, target):
        assert source.parent == target.parent == parent
        assert source.name.startswith(".eve-income-tracker-migrating-")
        assert json.loads((source / MARKER_NAME).read_text())["state"] == "ready"
        events.append(("rename", target))
        real_rename(source, target)

    def begin(target):
        assert read_data_root_marker(target.root).state == "ready"
        assert source_bytes(paths)[Path("config/local.key")] == b"private protected key"
        events.append(("begin", target.root))
        real_begin(target)

    def validator(destination):
        assert destination.database_path.exists()
        events.append(("validate", destination.data_root))

    def finalize():
        events.append(("finalize",))
        real_finalize()

    monkeypatch.setattr(data_migration, "write_staged_data_root_marker", marker)
    monkeypatch.setattr(data_migration.os, "rename", rename)
    monkeypatch.setattr(store, "begin_current_switch", begin)
    monkeypatch.setattr(store, "finalize_current_switch", finalize)
    service_for(paths, store, validator).migrate(
        reference, parent, create_recovery=False
    )
    assert events[0][0:1] == ("marker",)
    assert events[0][2] == "incomplete"
    stages = [item[0] for item in events]
    assert stages.index("validate") < stages.index("rename") < stages.index("begin")
    assert stages[-2:] == ["validate", "finalize"]


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("space", "space_insufficient"),
        ("copy", "copy_failed"),
        ("hash", "file_verification_failed"),
        ("sqlite", "database_invalid"),
        ("key", "destination_invalid"),
        ("marker", "marker_failed"),
        ("recovery", "recovery_failed"),
        ("rename", "rename_failed"),
        ("registry", "registry_failed"),
    ],
)
def test_precommit_faults_preserve_source_and_leave_no_ready_target(
    tmp_path, monkeypatch, fault, code
):
    paths, reference, store, parent = make_source(tmp_path)
    before = source_bytes(paths)
    service = service_for(paths, store)
    if fault == "space":
        monkeypatch.setattr(
            data_migration.shutil, "disk_usage", lambda _: SimpleNamespace(free=0)
        )
    elif fault == "copy":
        monkeypatch.setattr(data_migration.shutil, "copy2", fail)
    elif fault == "hash":
        real_copy = data_migration.shutil.copy2

        def corrupt(source, destination, **kwargs):
            result = real_copy(source, destination, **kwargs)
            Path(destination).write_bytes(b"corruption")
            return result

        monkeypatch.setattr(data_migration.shutil, "copy2", corrupt)
    elif fault == "sqlite":
        monkeypatch.setattr(data_migration, "_check_database", fail)
    elif fault == "key":
        service = service_for(paths, store, fail)
    elif fault == "marker":
        monkeypatch.setattr(data_migration, "write_staged_data_root_marker", fail)
    elif fault == "recovery":
        monkeypatch.setattr(RecoverySnapshotService, "create", fail)
    elif fault == "rename":
        monkeypatch.setattr(data_migration.os, "rename", fail)
    elif fault == "registry":
        real_begin = store.begin_current_switch

        def partial_registry(target):
            real_begin(target)
            fail()

        monkeypatch.setattr(store, "begin_current_switch", partial_registry)
    with pytest.raises(DataMigrationError) as caught:
        service.migrate(reference, parent, create_recovery=True)
    assert caught.value.code == code
    assert "secret" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert store.read_current() == reference
    assert store.read_pending_cleanup() is None
    assert source_bytes(paths) == before
    assert not (parent / "EVE Income Tracker").exists()
    assert not list(parent.glob(".eve-income-tracker-migrating-*"))


def test_reopen_failure_rolls_back_and_quarantines_failed_target(tmp_path):
    paths, reference, store, parent = make_source(tmp_path)
    before = source_bytes(paths)

    def validator(destination):
        if destination.data_root == parent / "EVE Income Tracker":
            raise ValueError("private DPAPI error")

    with pytest.raises(DataMigrationError) as caught:
        service_for(paths, store, validator).migrate(
            reference, parent, create_recovery=True
        )
    assert caught.value.code == "destination_reopen_failed"
    assert store.read_current() == reference
    assert source_bytes(paths) == before
    assert not (parent / "EVE Income Tracker").exists()
    quarantines = list(parent.glob(".eve-income-tracker-failed-*"))
    assert len(quarantines) == 1
    assert (
        quarantines[0] / "config/local.key"
    ).read_bytes() == b"private protected key"


def test_occupied_source_records_pending_cleanup_and_retries_exact_reference(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    service = service_for(paths, store)
    real_remove = data_migration.remove_owned_data_root
    monkeypatch.setattr(data_migration, "remove_owned_data_root", fail)
    result = service.migrate(reference, parent, create_recovery=False)
    assert result.original_removed is False
    assert result.pending_cleanup == PendingCleanup(reference)
    assert store.read_pending_cleanup() == PendingCleanup(reference)
    assert store.read_current() == result.reference
    assert paths.key_path.read_bytes() == b"private protected key"
    monkeypatch.setattr(data_migration, "remove_owned_data_root", real_remove)
    assert service.retry_pending_cleanup() is True
    assert store.read_pending_cleanup() is None
    assert not reference.root.exists()
    assert result.reference.root.exists()
    assert service.retry_pending_cleanup() is True


@pytest.mark.parametrize("damage", ["id", "marker", "current", "parent"])
def test_cleanup_never_deletes_mismatched_or_current_roots(tmp_path, damage):
    paths, reference, store, parent = make_source(tmp_path)
    service = service_for(paths, store)
    if damage == "id":
        pending = DataRootReference(reference.root, uuid4().hex, 1)
    elif damage == "parent":
        pending = DataRootReference(reference.root.parent, reference.root_id, 1)
    else:
        pending = reference
    store.write_pending_cleanup(PendingCleanup(pending))
    if damage != "current":
        target = DataRootReference(parent / "EVE Income Tracker", uuid4().hex, 1)
        target.root.mkdir()
        write_data_root_marker(
            target.root, DataRootMarker(target, "2026-10-02T00:00:00Z", "ready")
        )
        store.begin_current_switch(target)
        store.finalize_current_switch()
    if damage == "marker":
        (reference.root / MARKER_NAME).unlink()
    assert service.retry_pending_cleanup() is False
    assert paths.key_path.read_bytes() == b"private protected key"
    assert store.read_pending_cleanup() == PendingCleanup(pending)


@pytest.mark.parametrize(
    "target_kind", ["nonempty", "existing", "same", "dangerous", "nested"]
)
def test_invalid_targets_rejected_before_copy(tmp_path, monkeypatch, target_kind):
    paths, reference, store, parent = make_source(tmp_path)
    target = parent / "EVE Income Tracker"
    if target_kind == "nonempty":
        target.mkdir()
        (target / "foreign").write_bytes(b"keep")
    elif target_kind == "existing":
        target.mkdir()
        write_data_root_marker(
            target,
            DataRootMarker(
                DataRootReference(target, uuid4().hex, 1),
                "2026-10-02T00:00:00Z",
                "ready",
            ),
        )
    elif target_kind == "same":
        parent = reference.root.parent
    elif target_kind == "dangerous":
        parent = paths.resource_root
        parent.mkdir()
    elif target_kind == "nested":
        parent = reference.root / "extra"

    def forbidden_copy(*args, **kwargs):
        pytest.fail("invalid target reached copy")

    monkeypatch.setattr(data_migration.shutil, "copy2", forbidden_copy)
    with pytest.raises(DataMigrationError):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert store.read_current() == reference
    assert paths.key_path.exists()


def test_source_reparse_rejected_before_copy(tmp_path, monkeypatch):
    paths, reference, store, parent = make_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"keep")
    link = paths.data_root / "unexpected"
    # Junctions work on Windows without developer-mode symlink privileges.
    if os.name == "nt":
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:

        def forbidden_copy(*args, **kwargs):
            pytest.fail("reparse source reached copy")

        monkeypatch.setattr(data_migration.shutil, "copy2", forbidden_copy)
        with pytest.raises(DataMigrationError) as caught:
            service_for(paths, store).migrate(reference, parent, create_recovery=False)
        assert caught.value.code == "path_reparse"
        assert (outside / "private").read_bytes() == b"keep"
        assert list(parent.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_wal_commits_copied_with_backup_api_without_mutating_critical_source(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    with closing(sqlite3.connect(paths.database_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO income VALUES (73)")
        writer.commit()
        before_db = paths.database_path.read_bytes()
        wal = Path(str(paths.database_path) + "-wal")
        before_wal = wal.read_bytes()
        monkeypatch.setattr(data_migration, "remove_owned_data_root", fail)
        result = service_for(paths, store).migrate(
            reference, parent, create_recovery=True
        )
        assert paths.database_path.read_bytes() == before_db
        assert wal.read_bytes() == before_wal
    with closing(
        sqlite3.connect(result.reference.root / "data/eve_income.sqlite3")
    ) as db:
        assert db.execute("SELECT amount FROM income ORDER BY amount").fetchall() == [
            (42,),
            (73,),
        ]
    assert not (result.reference.root / "data/eve_income.sqlite3-wal").exists()


def test_missing_database_and_empty_key_do_not_create_empty_destination(tmp_path):
    paths, reference, store, parent = make_source(tmp_path)
    paths.database_path.unlink()
    with pytest.raises(DataMigrationError):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert not paths.database_path.exists()
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("when", ["ready-marker", "pending-write", "finalize-before"])
def test_late_precommit_failures_roll_back_and_clear_partial_cleanup(
    tmp_path, monkeypatch, when
):
    paths, reference, store, parent = make_source(tmp_path)
    before = source_bytes(paths)
    if when == "ready-marker":
        real_write = data_migration.write_staged_data_root_marker

        def write(staging, marker):
            if marker.state == "ready":
                fail()
            real_write(staging, marker)

        monkeypatch.setattr(data_migration, "write_staged_data_root_marker", write)
    elif when == "pending-write":
        from app.data_location_store import PENDING_CLEANUP_ROOT

        real_write = store.backend.write

        def write(name, value, kind):
            if name == PENDING_CLEANUP_ROOT:
                fail()
            real_write(name, value, kind)

        monkeypatch.setattr(store.backend, "write", write)
    else:
        monkeypatch.setattr(store, "finalize_current_switch", fail)
    with pytest.raises(DataMigrationError):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert store.read_current() == reference
    assert store.read_pending_cleanup() is None
    assert source_bytes(paths) == before
    assert not (parent / "EVE Income Tracker").exists()


def test_finalize_cleanup_error_after_commit_preserves_selected_target(
    tmp_path, monkeypatch
):
    from app.data_location_store import PREVIOUS_DATA_ROOT

    paths, reference, store, parent = make_source(tmp_path)
    real_delete = store.backend.delete

    def fail_committed_cleanup(name):
        if name == PREVIOUS_DATA_ROOT and store._committed():
            fail()
        real_delete(name)

    monkeypatch.setattr(store.backend, "delete", fail_committed_cleanup)
    with pytest.raises(DataMigrationError) as caught:
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert caught.value.code == "registry_finalize_failed"
    selected = store.read_current()
    assert selected.root == parent / "EVE Income Tracker"
    assert read_data_root_marker(selected.root).state == "ready"
    assert store.read_pending_cleanup() == PendingCleanup(reference)
    assert paths.key_path.exists()


def test_partial_delete_failure_is_retryable_through_service(tmp_path, monkeypatch):
    from app import data_roots

    paths, reference, store, parent = make_source(tmp_path)
    real_delete = data_roots._delete_quarantined_tree

    def partial_delete(root, identity):
        (root / MARKER_NAME).unlink()
        (root / "logs/app.log").unlink()
        fail()

    monkeypatch.setattr(data_roots, "_delete_quarantined_tree", partial_delete)
    service = service_for(paths, store)
    result = service.migrate(reference, parent, create_recovery=False)
    assert result.pending_cleanup == PendingCleanup(reference)
    assert read_data_root_marker(reference.root).reference == reference
    monkeypatch.setattr(data_roots, "_delete_quarantined_tree", real_delete)
    assert service.retry_pending_cleanup()
    assert not reference.root.exists()
    assert read_data_root_marker(result.reference.root).state == "ready"


def test_irrecoverable_cleanup_does_not_record_misleading_old_path(
    tmp_path, monkeypatch
):
    from app import data_roots

    paths, reference, store, parent = make_source(tmp_path)

    def occupy_original(root, identity):
        reference.root.mkdir()
        (reference.root / "foreign").write_bytes(b"keep")
        fail()

    monkeypatch.setattr(data_roots, "_delete_quarantined_tree", occupy_original)
    with pytest.raises(DataMigrationError) as caught:
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert caught.value.code == "cleanup_restore_failed"
    assert store.read_current().root == parent / "EVE Income Tracker"
    assert store.read_pending_cleanup() is None
    assert (reference.root / "foreign").read_bytes() == b"keep"
    assert list(reference.root.parent.glob(".eve-income-tracker-removing-*"))


def test_empty_formal_target_is_supported(tmp_path):
    paths, reference, store, parent = make_source(tmp_path)
    (parent / "EVE Income Tracker").mkdir()
    result = service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert read_data_root_marker(result.reference.root).state == "ready"


def test_target_created_during_copy_is_preserved(tmp_path, monkeypatch):
    paths, reference, store, parent = make_source(tmp_path)
    real_copy = data_migration.shutil.copy2
    target = parent / "EVE Income Tracker"

    def copy_and_occupy(source, destination, **kwargs):
        target.mkdir(exist_ok=True)
        (target / "foreign").write_bytes(b"keep")
        return real_copy(source, destination, **kwargs)

    monkeypatch.setattr(data_migration.shutil, "copy2", copy_and_occupy)
    with pytest.raises(DataMigrationError):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert (target / "foreign").read_bytes() == b"keep"
    assert store.read_current() == reference
    assert not list(parent.glob(".eve-income-tracker-migrating-*"))


def test_empty_key_rejected_before_copy(tmp_path, monkeypatch):
    paths, reference, store, parent = make_source(tmp_path)
    paths.key_path.write_bytes(b"")
    monkeypatch.setattr(
        data_migration.shutil, "copy2", lambda *a, **k: pytest.fail("copy")
    )
    with pytest.raises(DataMigrationError) as caught:
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert caught.value.code == "source_invalid"
    assert list(parent.iterdir()) == []


def test_pending_migration_consumed_even_if_old_root_is_occupied(tmp_path, monkeypatch):
    from app.data_location_store import PendingMigration

    paths, reference, store, parent = make_source(tmp_path)
    store.write_pending_migration(PendingMigration(parent, False))
    monkeypatch.setattr(data_migration, "remove_owned_data_root", fail)
    result = service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert store.read_current() == result.reference
    assert store.read_pending_migration() is None
    assert store.read_pending_cleanup() == PendingCleanup(reference)


def test_failed_registry_rollback_still_quarantines_unselected_formal_target(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    real_begin = store.begin_current_switch

    def partial_begin(target):
        real_begin(target)
        fail()

    monkeypatch.setattr(store, "begin_current_switch", partial_begin)
    monkeypatch.setattr(store, "rollback_current_switch", fail)
    with pytest.raises(DataMigrationError) as caught:
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert caught.value.code == "registry_rollback_failed"
    assert store.read_current() == reference
    assert paths.key_path.exists()
    assert not (parent / "EVE Income Tracker").exists()


def test_failed_cleanup_retry_restore_clears_misleading_original_reference(
    tmp_path, monkeypatch
):
    from app import data_roots

    paths, reference, store, parent = make_source(tmp_path)
    service = service_for(paths, store)
    with monkeypatch.context() as patch:
        patch.setattr(data_migration, "remove_owned_data_root", fail)
        service.migrate(reference, parent, create_recovery=False)

    def occupy_original(root, identity):
        reference.root.mkdir()
        (reference.root / "foreign").write_bytes(b"keep")
        fail()

    monkeypatch.setattr(data_roots, "_delete_quarantined_tree", occupy_original)
    assert service.retry_pending_cleanup() is False
    assert store.read_pending_cleanup() is None
    assert (reference.root / "foreign").read_bytes() == b"keep"
