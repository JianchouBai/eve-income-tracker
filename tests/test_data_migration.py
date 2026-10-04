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

from app import data_migration, windows_owned_delete
from app.data_location_store import (
    CURRENT_SWITCH_COMMITTED,
    DATA_ROOT,
    DATA_ROOT_ID,
    DATA_ROOT_SCHEMA,
    MIGRATION_TRANSACTION,
    PENDING_CLEANUP_ROOT,
    PENDING_CLEANUP_ROOT_ID,
    PENDING_MIGRATION_CREATE_RECOVERY,
    PENDING_MIGRATION_TARGET,
    PREVIOUS_DATA_ROOT,
    PREVIOUS_DATA_ROOT_ABSENT,
    PREVIOUS_DATA_ROOT_ID,
    PREVIOUS_DATA_ROOT_SCHEMA,
    MemoryDataLocationStore,
    MigrationTransaction,
    PendingCleanup,
    PendingMigration,
    WindowsDataLocationStore,
)
from app.data_migration import DataMigrationError, DataMigrationService
from app.data_roots import (
    MARKER_NAME,
    DataRootMarker,
    DataRootReference,
    DataRootValidationError,
    read_data_root_marker,
    validate_existing_data_root,
    write_data_root_marker,
    write_staged_data_root_marker,
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


class SimulatedProcessDeath(BaseException):
    pass


def restarted_service(paths, store):
    restarted_store = WindowsDataLocationStore(store.backend)
    return restarted_store, service_for(paths, restarted_store)


def record_attempt(
    source, store, parent, *, location="staging", identity=True, marker=True
):
    target = DataRootReference(parent / "EVE Income Tracker", uuid4().hex, 1)
    operation_id = uuid4().hex
    transaction = MigrationTransaction(
        1, operation_id, source, target, "2026-10-04T00:00:00Z", None
    )
    staging = parent / f".eve-income-tracker-migrating-{operation_id}"
    diagnostic = parent / f".eve-income-tracker-failed-{operation_id}"
    if location is not None:
        staging.mkdir()
        if identity:
            transaction = MigrationTransaction(
                transaction.version,
                transaction.operation_id,
                transaction.source,
                transaction.target,
                transaction.created_at,
                data_migration._directory_identity(staging),
            )
        if marker:
            write_staged_data_root_marker(
                staging, DataRootMarker(target, transaction.created_at, "ready")
            )
        if location == "formal":
            staging.rename(target.root)
        elif location == "diagnostic":
            staging.rename(diagnostic)
    store.write_migration_transaction(transaction)
    return transaction, staging, diagnostic


@pytest.mark.parametrize("source_state", ["offline", "marker", "strict-validator"])
def test_reconcile_keeps_attempt_when_selected_source_is_invalid(
    tmp_path, source_state
):
    paths, source, store, parent = make_source(tmp_path)
    transaction, _, _ = record_attempt(source, store, parent, location="formal")
    target = transaction.target.root
    (target / "attempt-bytes").write_bytes(b"keep")
    if source_state == "offline":
        paths.data_root.rename(paths.data_root.with_name("displaced-source"))
    elif source_state == "marker":
        (paths.data_root / MARKER_NAME).unlink()

    def strict_validator(destination):
        if destination.data_root == source.root:
            raise ValueError("source cannot be reopened")

    before_registry = store.backend.values.copy()
    restarted_store = WindowsDataLocationStore(store.backend)
    service = service_for(
        paths,
        restarted_store,
        strict_validator if source_state == "strict-validator" else None,
    )
    with pytest.raises((DataMigrationError, DataRootValidationError, ValueError)):
        service.reconcile_interrupted_migration()
    assert store.backend.values == before_registry
    assert (target / "attempt-bytes").read_bytes() == b"keep"
    assert restarted_store.read_migration_transaction() == transaction


def test_reconcile_keeps_transaction_when_target_parent_is_unavailable(tmp_path):
    paths, source, store, parent = make_source(tmp_path)
    transaction, _, _ = record_attempt(source, store, parent, location=None)
    parent.rmdir()
    before_registry = store.backend.values.copy()
    restarted_store, service = restarted_service(paths, store)
    with pytest.raises((DataMigrationError, DataRootValidationError, OSError)):
        service.reconcile_interrupted_migration()
    assert store.backend.values == before_registry
    assert restarted_store.read_migration_transaction() == transaction
    assert paths.key_path.read_bytes() == b"private protected key"


def test_cleanup_current_reference_waits_until_selected_root_is_valid(tmp_path):
    paths, source, store, _ = make_source(tmp_path)
    store.write_pending_cleanup(PendingCleanup(source))
    paths.data_root.rename(paths.data_root.with_name("displaced-source"))
    restarted_store, service = restarted_service(paths, store)
    assert service.retry_pending_cleanup() is False
    assert restarted_store.read_pending_cleanup() == PendingCleanup(source)


def test_reconcile_preserves_preexisting_empty_target_beside_staging(tmp_path):
    paths, source, store, parent = make_source(tmp_path)
    formal = parent / "EVE Income Tracker"
    formal.mkdir()
    formal_identity = data_migration._directory_identity(formal)
    _, staging, _ = record_attempt(source, store, parent)
    (staging / "attempt-bytes").write_bytes(b"discard")
    restarted_store, service = restarted_service(paths, store)
    assert service.reconcile_interrupted_migration() is True
    assert restarted_store.read_migration_transaction() is None
    assert data_migration._directory_identity(formal) == formal_identity
    assert list(formal.iterdir()) == []
    assert not staging.exists()
    assert paths.key_path.read_bytes() == b"private protected key"


def test_preexisting_empty_target_survives_crash_after_first_transaction_write(
    tmp_path, monkeypatch
):
    paths, source, store, parent = make_source(tmp_path)
    target = parent / "EVE Income Tracker"
    target.mkdir()
    target_identity = data_migration._directory_identity(target)
    real_write = store.backend.write
    with monkeypatch.context() as patch:

        def write_then_die(name, value, kind):
            real_write(name, value, kind)
            if name == MIGRATION_TRANSACTION:
                raise SimulatedProcessDeath

        patch.setattr(store.backend, "write", write_then_die)
        with pytest.raises(SimulatedProcessDeath):
            service_for(paths, store).migrate(source, parent, create_recovery=False)

    transaction = store.read_migration_transaction()
    staging = parent / f".eve-income-tracker-migrating-{transaction.operation_id}"
    assert transaction.staging_identity is None
    assert not staging.exists()
    restarted_store, service = restarted_service(paths, store)
    assert service.reconcile_interrupted_migration() is True
    assert restarted_store.read_migration_transaction() is None
    assert data_migration._directory_identity(target) == target_identity
    assert list(target.iterdir()) == []
    assert paths.key_path.read_bytes() == b"private protected key"
    _, second_restart = restarted_service(paths, store)
    assert second_restart.reconcile_interrupted_migration() is False


def test_preexisting_empty_target_survives_crash_after_staging_mkdir(
    tmp_path, monkeypatch
):
    paths, source, store, parent = make_source(tmp_path)
    target = parent / "EVE Income Tracker"
    target.mkdir()
    target_identity = data_migration._directory_identity(target)
    real_mkdir = Path.mkdir
    with monkeypatch.context() as patch:

        def mkdir_then_die(path, *args, **kwargs):
            result = real_mkdir(path, *args, **kwargs)
            if path.name.startswith(".eve-income-tracker-migrating-"):
                raise SimulatedProcessDeath
            return result

        patch.setattr(Path, "mkdir", mkdir_then_die)
        with pytest.raises(SimulatedProcessDeath):
            service_for(paths, store).migrate(source, parent, create_recovery=False)

    transaction = store.read_migration_transaction()
    staging = parent / f".eve-income-tracker-migrating-{transaction.operation_id}"
    assert transaction.staging_identity is None
    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    restarted_store, service = restarted_service(paths, store)
    assert service.reconcile_interrupted_migration() is True
    assert restarted_store.read_migration_transaction() is None
    assert not staging.exists()
    assert data_migration._directory_identity(target) == target_identity
    assert list(target.iterdir()) == []
    assert paths.key_path.read_bytes() == b"private protected key"
    _, second_restart = restarted_service(paths, store)
    assert second_restart.reconcile_interrupted_migration() is False


@pytest.mark.parametrize("location", ["staging", "formal", "diagnostic"])
@pytest.mark.parametrize("change", ["root-id", "created-at"])
def test_reconcile_refuses_changed_attempt_marker(tmp_path, location, change):
    paths, source, store, parent = make_source(tmp_path)
    transaction, staging, diagnostic = record_attempt(
        source, store, parent, location=location
    )
    attempt = (
        staging
        if location == "staging"
        else transaction.target.root
        if location == "formal"
        else diagnostic
    )
    marker_path = attempt / MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["root_id" if change == "root-id" else "created_at"] = (
        uuid4().hex if change == "root-id" else "2026-10-03T00:00:00Z"
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    (attempt / "attempt-bytes").write_bytes(b"keep")
    restarted_store, service = restarted_service(paths, store)
    with pytest.raises((DataMigrationError, DataRootValidationError)):
        service.reconcile_interrupted_migration()
    assert (attempt / "attempt-bytes").read_bytes() == b"keep"
    assert restarted_store.read_migration_transaction() == transaction


def test_reconcile_refuses_nonempty_markerless_diagnostic(tmp_path):
    paths, source, store, parent = make_source(tmp_path)
    transaction, _, diagnostic = record_attempt(
        source, store, parent, location="diagnostic"
    )
    (diagnostic / MARKER_NAME).unlink()
    (diagnostic / "foreign").write_bytes(b"keep")
    restarted_store, service = restarted_service(paths, store)
    with pytest.raises(DataMigrationError, match="cleanup_failed"):
        service.reconcile_interrupted_migration()
    assert (diagnostic / "foreign").read_bytes() == b"keep"
    assert restarted_store.read_migration_transaction() == transaction


@pytest.mark.parametrize("target_state", ["occupied", "replaced-formal"])
def test_reconcile_refuses_foreign_target_and_preserves_bytes(tmp_path, target_state):
    paths, source, store, parent = make_source(tmp_path)
    transaction, staging, _ = record_attempt(
        source,
        store,
        parent,
        location="staging" if target_state == "occupied" else "formal",
    )
    target = transaction.target.root
    if target_state == "occupied":
        target.mkdir()
    else:
        target.rename(parent / "displaced-formal")
    if target_state == "replaced-formal":
        target.mkdir()
    (target / "foreign").write_bytes(b"keep")
    restarted_store, service = restarted_service(paths, store)
    with pytest.raises(DataMigrationError, match="cleanup_failed"):
        service.reconcile_interrupted_migration()
    assert (target / "foreign").read_bytes() == b"keep"
    assert restarted_store.read_migration_transaction() == transaction
    if target_state == "occupied":
        assert staging.is_dir()
    else:
        assert (parent / "displaced-formal" / MARKER_NAME).is_file()


def test_failed_migration_does_not_discard_staging_with_changed_marker(
    tmp_path, monkeypatch
):
    paths, source, store, parent = make_source(tmp_path)

    def corrupt_marker_then_fail(_, source_paths, destination):
        marker_path = destination.data_root / MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["root_id"] = uuid4().hex
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        (destination.data_root / "attempt-bytes").write_bytes(b"keep")
        raise ValueError("copy failed after foreign replacement")

    monkeypatch.setattr(DataMigrationService, "_copy", corrupt_marker_then_fail)
    with pytest.raises(DataMigrationError, match="cleanup_failed"):
        service_for(paths, store).migrate(source, parent, create_recovery=False)
    transaction = store.read_migration_transaction()
    staging = parent / f".eve-income-tracker-migrating-{transaction.operation_id}"
    assert (staging / "attempt-bytes").read_bytes() == b"keep"
    assert store.read_current() == source


@pytest.mark.parametrize("location", ["formal", "diagnostic"])
def test_reconcile_never_deletes_artifact_without_durable_identity(tmp_path, location):
    paths, source, store, parent = make_source(tmp_path)
    transaction, _, diagnostic = record_attempt(
        source, store, parent, location=location, identity=False, marker=False
    )
    artifact = transaction.target.root if location == "formal" else diagnostic
    if location == "formal":
        (artifact / "foreign").write_bytes(b"keep")
    restarted_store, service = restarted_service(paths, store)
    with pytest.raises((DataMigrationError, DataRootValidationError)):
        service.reconcile_interrupted_migration()
    assert artifact.is_dir()
    if location == "formal":
        assert (artifact / "foreign").read_bytes() == b"keep"
    assert restarted_store.read_migration_transaction() == transaction


@pytest.mark.parametrize("boundary", ["rename", "child", "marker", "root"])
def test_formal_cleanup_restarts_after_each_mutation(tmp_path, monkeypatch, boundary):
    paths, source, store, parent = make_source(tmp_path)
    transaction, _, diagnostic = record_attempt(
        source, store, parent, location="formal"
    )
    target = transaction.target.root
    (target / "attempt-bytes").write_bytes(b"discard")
    restarted_store, service = restarted_service(paths, store)
    with monkeypatch.context() as patch:
        if boundary == "rename":
            real_rename = data_migration.os.rename

            def rename_then_die(old, new):
                real_rename(old, new)
                if old == target and new == diagnostic:
                    raise SimulatedProcessDeath

            patch.setattr(data_migration.os, "rename", rename_then_die)
        else:
            real_delete = windows_owned_delete._delete_opened
            crash_path = (
                diagnostic / "attempt-bytes"
                if boundary == "child"
                else diagnostic / MARKER_NAME
                if boundary == "marker"
                else diagnostic
            )

            def delete_then_die(kernel, path, handle, *args, **kwargs):
                result = real_delete(kernel, path, handle, *args, **kwargs)
                if path == crash_path:
                    raise SimulatedProcessDeath
                return result

            patch.setattr(windows_owned_delete, "_delete_opened", delete_then_die)
        with pytest.raises(SimulatedProcessDeath):
            service.reconcile_interrupted_migration()
    assert restarted_store.read_migration_transaction() == transaction
    assert not target.exists()
    restarted_store, service = restarted_service(paths, store)
    assert service.reconcile_interrupted_migration() is True
    assert restarted_store.read_migration_transaction() is None
    assert not diagnostic.exists()
    assert paths.key_path.read_bytes() == b"private protected key"


REGISTRY_CRASH_BOUNDARIES = [
    ("delete", PREVIOUS_DATA_ROOT, 1, False),
    ("delete", PREVIOUS_DATA_ROOT_ID, 1, False),
    ("delete", PREVIOUS_DATA_ROOT_SCHEMA, 1, False),
    ("delete", PREVIOUS_DATA_ROOT_ABSENT, 1, False),
    ("write", MIGRATION_TRANSACTION, 1, False),
    ("write", MIGRATION_TRANSACTION, 2, False),
    ("write", PREVIOUS_DATA_ROOT_ID, 1, False),
    ("write", PREVIOUS_DATA_ROOT_SCHEMA, 1, False),
    ("write", PREVIOUS_DATA_ROOT, 1, False),
    ("write", DATA_ROOT_SCHEMA, 1, False),
    ("write", DATA_ROOT_ID, 1, False),
    ("write", DATA_ROOT, 1, False),
    ("write", PENDING_CLEANUP_ROOT_ID, 1, False),
    ("write", PENDING_CLEANUP_ROOT, 1, False),
    ("write", CURRENT_SWITCH_COMMITTED, 1, True),
    ("delete", PREVIOUS_DATA_ROOT, 2, True),
    ("delete", PREVIOUS_DATA_ROOT_ID, 2, True),
    ("delete", PREVIOUS_DATA_ROOT_SCHEMA, 2, True),
    ("delete", PREVIOUS_DATA_ROOT_ABSENT, 2, True),
    ("delete", CURRENT_SWITCH_COMMITTED, 1, True),
    ("delete", PENDING_MIGRATION_TARGET, 1, True),
    ("delete", PENDING_MIGRATION_CREATE_RECOVERY, 1, True),
    ("delete", MIGRATION_TRANSACTION, 1, True),
]


def test_transaction_is_durable_before_staging_directory_creation(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    store.write_pending_migration(PendingMigration(parent, False))
    real_mkdir = Path.mkdir

    def crash_before_staging(path, *args, **kwargs):
        if path.name.startswith(".eve-income-tracker-migrating-"):
            transaction = store.read_migration_transaction()
            assert transaction.source == reference
            assert transaction.target.root == parent / "EVE Income Tracker"
            assert transaction.staging_identity is None
            raise SimulatedProcessDeath
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", crash_before_staging)
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    restarted_store, restarted = restarted_service(paths, store)
    restarted.reconcile_interrupted_migration()
    assert restarted_store.read_current() == reference
    assert restarted_store.read_migration_transaction() is None
    assert restarted_store.read_pending_migration() == PendingMigration(parent, False)
    assert list(parent.iterdir()) == []


def test_staging_identity_is_durable_before_first_staging_bytes(tmp_path, monkeypatch):
    paths, reference, store, parent = make_source(tmp_path)

    def crash_before_marker(staging, marker):
        transaction = store.read_migration_transaction()
        assert transaction.staging_identity == data_migration._directory_identity(
            staging
        )
        raise SimulatedProcessDeath

    monkeypatch.setattr(
        data_migration, "write_staged_data_root_marker", crash_before_marker
    )
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    transaction = store.read_migration_transaction()
    staging = parent / f".eve-income-tracker-migrating-{transaction.operation_id}"
    assert staging.is_dir()
    restarted_store, restarted = restarted_service(paths, store)
    restarted.reconcile_interrupted_migration()
    assert restarted_store.read_current() == reference
    assert restarted_store.read_migration_transaction() is None
    assert not staging.exists()


def test_crash_after_formal_rename_reconciles_precommit_operation(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    store.write_pending_migration(PendingMigration(parent, False))
    real_rename = data_migration.os.rename

    def rename_then_crash(source, target):
        real_rename(source, target)
        if source.name.startswith(".eve-income-tracker-migrating-"):
            raise SimulatedProcessDeath

    monkeypatch.setattr(data_migration.os, "rename", rename_then_crash)
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert (parent / "EVE Income Tracker").is_dir()
    restarted_store, restarted = restarted_service(paths, store)
    restarted.reconcile_interrupted_migration()
    assert restarted_store.read_current() == reference
    assert restarted_store.read_migration_transaction() is None
    assert restarted_store.read_pending_migration() == PendingMigration(parent, False)
    assert not (parent / "EVE Income Tracker").exists()
    assert paths.key_path.read_bytes() == b"private protected key"


@pytest.mark.parametrize(
    ("method", "field", "occurrence", "committed"),
    REGISTRY_CRASH_BOUNDARIES,
)
def test_every_registry_mutation_crash_boundary_converges_on_fresh_instance(
    tmp_path, monkeypatch, method, field, occurrence, committed
):
    paths, reference, store, parent = make_source(tmp_path)
    request = PendingMigration(parent, False)
    store.write_pending_migration(request)
    backend = store.backend
    real_mutation = getattr(backend, method)
    matched = 0

    def crash_after_mutation(name, *args):
        nonlocal matched
        result = real_mutation(name, *args)
        if name == field:
            matched += 1
            if matched == occurrence:
                raise SimulatedProcessDeath
        return result

    monkeypatch.setattr(backend, method, crash_after_mutation)
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert matched == occurrence
    monkeypatch.undo()
    restarted_store, restarted = restarted_service(paths, store)
    restarted.reconcile_interrupted_migration()
    assert restarted.reconcile_interrupted_migration() is False
    assert restarted_store.read_migration_transaction() is None
    if committed:
        current = restarted_store.read_current()
        assert current.root == parent / "EVE Income Tracker"
        assert restarted_store.read_pending_migration() is None
        assert restarted_store.read_pending_cleanup() == PendingCleanup(reference)
        assert paths.data_root.exists()
        assert restarted.retry_pending_cleanup() is True
        assert not paths.data_root.exists()
    else:
        assert restarted_store.read_current() == reference
        assert restarted_store.read_pending_migration() == request
        assert restarted_store.read_pending_cleanup() is None
        assert paths.key_path.read_bytes() == b"private protected key"
        assert not (parent / "EVE Income Tracker").exists()
    assert not list(parent.glob(".eve-income-tracker-migrating-*"))
    assert not list(parent.glob(".eve-income-tracker-failed-*"))


def test_committed_target_reconstructs_missing_cleanup_before_retiring_transaction(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    store.write_pending_migration(PendingMigration(parent, False))
    real_write = store.backend.write

    def crash_after_commit(name, value, kind):
        real_write(name, value, kind)
        if name == CURRENT_SWITCH_COMMITTED:
            raise SimulatedProcessDeath

    monkeypatch.setattr(store.backend, "write", crash_after_commit)
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    monkeypatch.undo()
    store.clear_pending_cleanup()
    restarted_store, restarted = restarted_service(paths, store)

    assert restarted.reconcile_interrupted_migration() is True
    assert restarted_store.read_pending_cleanup() == PendingCleanup(reference)
    assert restarted_store.read_migration_transaction() is None
    assert restarted_store.read_current().root == parent / "EVE Income Tracker"
    assert paths.data_root.exists()


def test_reconciliation_refuses_foreign_replacement_at_owned_staging_path(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)

    def crash_before_marker(staging, marker):
        raise SimulatedProcessDeath

    monkeypatch.setattr(
        data_migration, "write_staged_data_root_marker", crash_before_marker
    )
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    monkeypatch.undo()
    transaction = store.read_migration_transaction()
    staging = parent / f".eve-income-tracker-migrating-{transaction.operation_id}"
    staging.rmdir()
    staging.mkdir()
    (staging / "foreign").write_bytes(b"keep")
    restarted_store, restarted = restarted_service(paths, store)

    with pytest.raises(DataMigrationError, match="cleanup_failed"):
        restarted.reconcile_interrupted_migration()
    assert (staging / "foreign").read_bytes() == b"keep"
    assert restarted_store.read_current() == reference
    assert restarted_store.read_migration_transaction() == transaction
    assert paths.key_path.read_bytes() == b"private protected key"
    with pytest.raises(DataMigrationError, match="migration_recovery_pending"):
        restarted.migrate(reference, parent, create_recovery=False)
    assert (staging / "foreign").read_bytes() == b"keep"
    assert restarted_store.read_migration_transaction() == transaction


def test_committed_reconciliation_refuses_target_directory_replacement(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    store.write_pending_migration(PendingMigration(parent, False))
    real_write = store.backend.write

    def crash_after_commit(name, value, kind):
        real_write(name, value, kind)
        if name == CURRENT_SWITCH_COMMITTED:
            raise SimulatedProcessDeath

    monkeypatch.setattr(store.backend, "write", crash_after_commit)
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    monkeypatch.undo()
    transaction = store.read_migration_transaction()
    target = transaction.target.root
    displaced = target.with_name(".displaced-transaction-target")
    target.rename(displaced)
    target.mkdir()
    write_data_root_marker(
        target,
        DataRootMarker(transaction.target, transaction.created_at, "ready"),
    )
    restarted_store, restarted = restarted_service(paths, store)

    with pytest.raises(DataMigrationError, match="registry_state_unknown"):
        restarted.reconcile_interrupted_migration()
    assert restarted_store.read_migration_transaction() == transaction
    assert restarted_store.read_current() == transaction.target
    assert paths.key_path.read_bytes() == b"private protected key"
    assert displaced.is_dir()


def test_reconciliation_fails_closed_when_current_is_neither_transaction_endpoint(
    tmp_path, monkeypatch
):
    paths, reference, store, parent = make_source(tmp_path)
    real_mkdir = Path.mkdir

    def crash_before_staging(path, *args, **kwargs):
        if path.name.startswith(".eve-income-tracker-migrating-"):
            raise SimulatedProcessDeath
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", crash_before_staging)
    with pytest.raises(SimulatedProcessDeath):
        service_for(paths, store).migrate(reference, parent, create_recovery=False)
    monkeypatch.undo()
    transaction = store.read_migration_transaction()
    foreign_parent = tmp_path / "foreign-volume"
    foreign_parent.mkdir()
    foreign = DataRootReference(foreign_parent / "EVE Income Tracker", uuid4().hex, 1)
    foreign.root.mkdir()
    write_data_root_marker(
        foreign.root, DataRootMarker(foreign, "2026-10-04T00:00:00Z", "ready")
    )
    store.begin_current_switch(foreign)
    store.finalize_current_switch()
    restarted_store, restarted = restarted_service(paths, store)

    with pytest.raises(DataMigrationError, match="registry_state_unknown"):
        restarted.reconcile_interrupted_migration()
    assert restarted_store.read_current() == foreign
    assert restarted_store.read_migration_transaction() == transaction
    assert paths.key_path.read_bytes() == b"private protected key"
    assert foreign.root.exists()


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
        if source == reference.root:
            assert target.parent == reference.root.parent
            assert target.name.startswith(".eve-income-tracker-removing-")
            events.append(("source-rename", target))
            real_rename(source, target)
            return
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
    assert stages[-3:] == ["validate", "finalize", "source-rename"]


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
    real_resume = data_migration.resume_owned_data_root_removal
    monkeypatch.setattr(data_migration, "resume_owned_data_root_removal", fail)
    result = service.migrate(reference, parent, create_recovery=False)
    assert result.original_removed is False
    assert result.pending_cleanup == store.read_pending_cleanup()
    assert result.pending_cleanup.reference == reference
    assert result.pending_cleanup.removal is not None
    assert store.read_current() == result.reference
    assert paths.key_path.read_bytes() == b"private protected key"
    monkeypatch.setattr(data_migration, "resume_owned_data_root_removal", real_resume)
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
    assert service.retry_pending_cleanup() is (damage == "current")
    assert paths.key_path.read_bytes() == b"private protected key"
    assert store.read_pending_cleanup() == (
        None if damage == "current" else PendingCleanup(pending)
    )


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
        monkeypatch.setattr(data_migration, "resume_owned_data_root_removal", fail)
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
    real_delete = data_roots.delete_tree_bound

    def partial_delete(root, identity, *, marker_last=None):
        assert marker_last == MARKER_NAME
        (root / "logs/app.log").unlink()
        fail()

    monkeypatch.setattr(data_roots, "delete_tree_bound", partial_delete)
    service = service_for(paths, store)
    result = service.migrate(reference, parent, create_recovery=False)
    assert result.pending_cleanup == store.read_pending_cleanup()
    assert result.pending_cleanup.removal is not None
    quarantine = result.pending_cleanup.removal.quarantine
    assert (quarantine / MARKER_NAME).exists()
    monkeypatch.setattr(data_roots, "delete_tree_bound", real_delete)
    assert service.retry_pending_cleanup()
    assert not reference.root.exists()
    assert read_data_root_marker(result.reference.root).state == "ready"


def test_irrecoverable_cleanup_does_not_record_misleading_old_path(
    tmp_path, monkeypatch
):
    from app import data_roots

    paths, reference, store, parent = make_source(tmp_path)

    def occupy_original(root, identity, *, marker_last=None):
        reference.root.mkdir()
        (reference.root / "foreign").write_bytes(b"keep")
        fail()

    monkeypatch.setattr(data_roots, "delete_tree_bound", occupy_original)
    result = service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert not result.original_removed
    assert store.read_current().root == parent / "EVE Income Tracker"
    assert store.read_pending_cleanup() == result.pending_cleanup
    assert result.pending_cleanup.removal.quarantine.exists()
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
    monkeypatch.setattr(data_migration, "resume_owned_data_root_removal", fail)
    result = service_for(paths, store).migrate(reference, parent, create_recovery=False)
    assert store.read_current() == result.reference
    assert store.read_pending_migration() is None
    assert store.read_pending_cleanup() == result.pending_cleanup
    assert result.pending_cleanup.removal is not None


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
        patch.setattr(data_migration, "resume_owned_data_root_removal", fail)
        service.migrate(reference, parent, create_recovery=False)

    def occupy_original(root, identity, *, marker_last=None):
        reference.root.mkdir()
        (reference.root / "foreign").write_bytes(b"keep")
        fail()

    monkeypatch.setattr(data_roots, "delete_tree_bound", occupy_original)
    assert service.retry_pending_cleanup() is False
    pending = store.read_pending_cleanup()
    assert pending is not None and pending.removal is not None
    assert pending.removal.quarantine.exists()
    assert (reference.root / "foreign").read_bytes() == b"keep"


@pytest.mark.parametrize("clear_field", ["PendingCleanupRoot", "PendingCleanupRootId"])
def test_successful_deletion_with_interrupted_journal_clear_converges(
    tmp_path, monkeypatch, clear_field
):
    paths, reference, store, parent = make_source(tmp_path)
    service = service_for(paths, store)
    real_delete = store.backend.delete
    with monkeypatch.context() as patch:

        def interrupt(name):
            if name == clear_field:
                fail()
            real_delete(name)

        patch.setattr(store.backend, "delete", interrupt)
        with pytest.raises(DataMigrationError) as caught:
            service.migrate(reference, parent, create_recovery=False)
        assert caught.value.code == "cleanup_state_failed"
    assert not reference.root.exists()
    assert reference.root.parent.is_dir()
    assert store.read_current().root == parent / "EVE Income Tracker"
    assert service.retry_pending_cleanup() is True
    assert store.read_pending_cleanup() is None
    assert "PendingCleanupRootId" not in store.backend.values
    assert service.retry_pending_cleanup() is True
    next_parent = tmp_path / "volume-next"
    next_parent.mkdir()
    result = service.migrate(store.read_current(), next_parent, create_recovery=False)
    assert result.original_removed


@pytest.mark.parametrize("parent_state", ["offline", "denied", "protected", "reparse"])
def test_missing_cleanup_root_is_not_completed_on_unavailable_or_unsafe_parent(
    tmp_path, monkeypatch, parent_state
):
    from app import data_roots

    paths, reference, store, parent = make_source(tmp_path)
    service = service_for(paths, store)
    real_remove = data_roots.remove_owned_data_root
    with monkeypatch.context() as patch:
        patch.setattr(data_migration, "resume_owned_data_root_removal", fail)
        service.migrate(reference, parent, create_recovery=False)
    real_remove(reference)
    old_parent = reference.root.parent
    link = None
    if parent_state == "offline":
        old_parent.rmdir()
    elif parent_state == "denied":
        real_stat = Path.stat

        def denied(path, **kwargs):
            if path == old_parent:
                raise PermissionError("offline or denied")
            return real_stat(path, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)
    elif parent_state == "protected":
        monkeypatch.setattr(data_roots, "PROJECT_ROOT", old_parent)
    else:
        outside = tmp_path / "outside-parent"
        outside.mkdir()
        old_parent.rmdir()
        link = old_parent
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
        assert service.retry_pending_cleanup() is False
        pending = store.read_pending_cleanup()
        assert pending is not None and pending.reference == reference
        assert pending.removal is not None
        assert store.read_current().root == parent / "EVE Income Tracker"
    finally:
        if link is not None:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()


def test_migration_excludes_only_owned_recovery_temp_files_in_backups(tmp_path):
    paths, reference, store, parent = make_source(tmp_path)
    service = service_for(paths, store)
    baseline = service.estimate(reference, parent, create_recovery=False).copy_bytes
    omitted = [
        "backups/recovery-db-abcdefgh.tmp",
        "backups/recovery-archive-12345678.tmp",
        "backups/recovery-update-abc_1234.tmp",
    ]
    kept = [
        "backups/recovery-db-short.tmp",
        "backups/recovery-db-ABCDEFGHI.tmp",
        "backups/ordinary-abcdefgh.tmp",
        "logs/recovery-db-abcdefgh.tmp",
        "backups/nested/recovery-db-abcdefgh.tmp",
        "backups/recovery-db-zyxwvuts.tmp/owned-by-user",
    ]
    for name in omitted + kept:
        path = paths.data_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    estimate = service.estimate(reference, parent, create_recovery=False)
    assert estimate.copy_bytes == baseline + 24
    result = service.migrate(reference, parent, create_recovery=False)
    for name in omitted:
        assert not (result.reference.root / name).exists()
    for name in kept:
        assert (result.reference.root / name).read_bytes() == b"keep"
