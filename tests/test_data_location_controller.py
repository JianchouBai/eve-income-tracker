"""Startup location orchestration with isolated files and no native UI."""

import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from app.data_location_controller import DataLocationController, DataLocationError
from app.data_location_store import MemoryDataLocationStore, PendingCleanup
from app.data_roots import (
    MARKER_NAME,
    DataRootMarker,
    DataRootReference,
    read_data_root_marker,
    write_data_root_marker,
)
from app.migration_recovery import RecoverySnapshotService
from app.paths import resolve_app_paths


class Interaction:
    def __init__(
        self,
        *,
        fresh="default",
        legacy="keep",
        parent=None,
        confirm=True,
        actions=(),
        roots=(),
    ):
        self.fresh, self.legacy = fresh, legacy
        self.parent, self.confirm = parent, confirm
        self.actions, self.roots = iter(actions), iter(roots)
        self.confirmed = []
        self.errors = []

    def choose_initial_location(self, default_root):
        return self.fresh

    def choose_legacy_action(self, root):
        return self.legacy

    def choose_data_parent(self):
        return self.parent

    def confirm_location(self, root, estimate=None):
        self.confirmed.append((root, estimate))
        return self.confirm

    def ask_retry_locate_exit(self, code):
        self.errors.append(code)
        action = next(self.actions, "exit")
        return action() if callable(action) else action

    def choose_existing_data_root(self):
        return next(self.roots, None)


def make_controller(tmp_path, store=None, validator=None):
    parent = tmp_path / "local"
    parent.mkdir(exist_ok=True)

    def validate(paths):
        assert paths.key_path.read_bytes() == b"protected-key"
        with closing(
            sqlite3.connect(paths.database_path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            assert db.execute("SELECT value FROM records").fetchall() == [(42,)]

    return DataLocationController(
        store or MemoryDataLocationStore(),
        resource_root=tmp_path / "program",
        default_parent=parent,
        existing_validator=validator or validate,
    )


def existing(tmp_path, name="local", *, marked=True, root_id=None):
    paths = resolve_app_paths(
        frozen=True,
        bundle_root=tmp_path / "program",
        data_root=tmp_path / name / "EVE Income Tracker",
    )
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.config_dir.mkdir()
    paths.key_path.write_bytes(b"protected-key")
    with closing(sqlite3.connect(paths.database_path)) as db:
        db.execute("CREATE TABLE records(value INTEGER)")
        db.execute("INSERT INTO records VALUES(42)")
        db.commit()
    ref = DataRootReference(paths.data_root, root_id or uuid4().hex, 1)
    if marked:
        write_data_root_marker(
            ref.root, DataRootMarker(ref, "2026-10-02T00:00:00Z", "ready")
        )
    return paths, ref


def select(store, ref):
    store.begin_current_switch(ref)
    store.finalize_current_switch()


@pytest.mark.parametrize("custom", [False, True])
def test_fresh_selection_prepares_only_marked_root(tmp_path, monkeypatch, custom):
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    parent = tmp_path / ("custom" if custom else "local")
    parent.mkdir(exist_ok=True)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: pytest.fail("database opened")
    )
    interaction = Interaction(fresh="custom" if custom else "default", parent=parent)
    ref = controller.resolve_startup(tmp_path / "local/EVE Income Tracker", interaction)
    assert ref.root == parent / "EVE Income Tracker"
    assert store.read_current() == ref
    assert read_data_root_marker(ref.root).reference == ref
    assert list(ref.root.iterdir()) == [ref.root / MARKER_NAME]
    assert interaction.confirmed == [(ref.root, None)]


@pytest.mark.parametrize(
    "fresh,parent,confirm",
    [
        ("exit", None, True),
        ("custom", None, True),
        ("default", None, False),
    ],
)
def test_fresh_cancellation_has_no_root_or_registry_residue(
    tmp_path, fresh, parent, confirm
):
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    with pytest.raises(DataLocationError, match="startup_cancelled"):
        controller.resolve_startup(
            tmp_path / "local/EVE Income Tracker",
            Interaction(fresh=fresh, parent=parent, confirm=confirm),
        )
    assert list((tmp_path / "local").iterdir()) == []
    assert store.backend.values == {}


def test_legacy_keep_marks_exact_layout_without_touching_user_files(tmp_path):
    paths, _ = existing(tmp_path, marked=False)
    before = paths.database_path.read_bytes(), paths.key_path.read_bytes()
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    ref = controller.resolve_startup(paths.data_root, Interaction())
    assert (
        ref == store.read_current() == read_data_root_marker(paths.data_root).reference
    )
    assert (paths.database_path.read_bytes(), paths.key_path.read_bytes()) == before


@pytest.mark.parametrize(
    "kind", ["no_database", "wrong_location", "missing_key", "invalid_database"]
)
def test_unrecognized_legacy_is_never_marked(tmp_path, kind):
    paths, _ = existing(
        tmp_path, "elsewhere" if kind == "wrong_location" else "local", marked=False
    )
    if kind == "no_database":
        paths.database_path.unlink()
    elif kind == "missing_key":
        paths.key_path.unlink()
    elif kind == "invalid_database":
        paths.database_path.write_bytes(b"private broken database")
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    with pytest.raises(DataLocationError):
        controller.resolve_startup(paths.data_root, Interaction(fresh="exit"))
    assert not (paths.data_root / MARKER_NAME).exists()
    assert store.backend.values == {}


@pytest.mark.parametrize("choice", ["exit", "migrate"])
def test_cancelled_legacy_flow_does_not_even_write_marker(tmp_path, choice):
    paths, _ = existing(tmp_path, marked=False)
    store = MemoryDataLocationStore()
    with pytest.raises(DataLocationError, match="startup_cancelled"):
        make_controller(tmp_path, store).resolve_startup(
            paths.data_root, Interaction(legacy=choice, parent=None)
        )
    assert not (paths.data_root / MARKER_NAME).exists()
    assert store.backend.values == {}


def test_legacy_migration_is_scheduled_then_executed_with_recovery(tmp_path):
    paths, _ = existing(tmp_path, marked=False)
    parent = tmp_path / "new"
    parent.mkdir()
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    ref = controller.resolve_startup(
        paths.data_root, Interaction(legacy="migrate", parent=parent)
    )
    assert ref.root == paths.data_root
    assert store.read_pending_migration().target_parent == parent
    result = controller.execute_pending_migration()
    assert result.reference == store.read_current()
    assert result.reference.root == parent / "EVE Income Tracker"
    assert not paths.data_root.exists()
    status = controller.status()
    assert status.recovery.successful_starts == 0
    assert controller.execute_pending_migration() is None


@pytest.mark.parametrize("damage", ["missing", "marker", "database", "key"])
def test_invalid_current_exits_without_default_or_database_creation(
    tmp_path, monkeypatch, damage
):
    paths, ref = existing(tmp_path, "offline")
    store = MemoryDataLocationStore()
    select(store, ref)
    if damage == "missing":
        paths.data_root.rename(paths.data_root.with_name("disconnected"))
    elif damage == "marker":
        (paths.data_root / MARKER_NAME).unlink()
    elif damage == "database":
        paths.database_path.unlink()
    else:
        paths.key_path.unlink()
    controller = make_controller(tmp_path, store)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: pytest.fail("database opened")
    )
    with pytest.raises(DataLocationError, match="startup_cancelled"):
        controller.resolve_startup(tmp_path / "local/EVE Income Tracker", Interaction())
    assert store.read_current() == ref
    assert list((tmp_path / "local").iterdir()) == []


def test_retry_rechecks_current_location(tmp_path):
    _, ref = existing(tmp_path, "offline")
    store = MemoryDataLocationStore()
    select(store, ref)
    marker = read_data_root_marker(ref.root)
    (ref.root / MARKER_NAME).unlink()

    def reconnect_drive():
        write_data_root_marker(ref.root, marker)
        return "retry"

    controller = make_controller(tmp_path, store)
    interaction = Interaction(actions=[reconnect_drive])
    assert (
        controller.resolve_startup(tmp_path / "local/EVE Income Tracker", interaction)
        == ref
    )
    assert len(interaction.errors) == 1


@pytest.mark.parametrize(
    "kind", ["empty", "wrong_id", "invalid_database", "incomplete"]
)
def test_locate_rejects_untrusted_roots_without_switching(tmp_path, kind):
    _, ref = existing(tmp_path, "old")
    paths, candidate = existing(tmp_path, "candidate", root_id=ref.root_id)
    store = MemoryDataLocationStore()
    select(store, ref)
    if kind == "empty":
        candidate = DataRootReference(
            tmp_path / "empty/EVE Income Tracker", ref.root_id, 1
        )
        candidate.root.mkdir(parents=True)
    elif kind == "wrong_id":
        candidate = DataRootReference(paths.data_root, uuid4().hex, 1)
        write_data_root_marker(
            candidate.root, DataRootMarker(candidate, "2026-10-02T00:00:00Z", "ready")
        )
    elif kind == "invalid_database":
        paths.database_path.write_bytes(b"secret invalid database")
    else:
        write_data_root_marker(
            candidate.root,
            DataRootMarker(candidate, "2026-10-02T00:00:00Z", "incomplete"),
        )
    with pytest.raises(DataLocationError) as caught:
        make_controller(tmp_path, store).reconnect_existing(candidate.root)
    assert "secret" not in str(caught.value)
    assert store.read_current() == ref


def test_locate_existing_reconnects_matching_id_and_cancel_exits(tmp_path):
    _, ref = existing(tmp_path, "old")
    paths, candidate = existing(tmp_path, "found", root_id=ref.root_id)
    store = MemoryDataLocationStore()
    select(store, ref)
    (ref.root / MARKER_NAME).unlink()
    controller = make_controller(tmp_path, store)
    interaction = Interaction(actions=["locate"], roots=[paths.data_root])
    assert (
        controller.resolve_startup(tmp_path / "local/EVE Income Tracker", interaction)
        == candidate
    )
    assert store.read_current() == candidate
    (candidate.root / MARKER_NAME).unlink()
    with pytest.raises(DataLocationError, match="startup_cancelled"):
        controller.resolve_startup(
            tmp_path / "local/EVE Income Tracker", Interaction(actions=["locate"])
        )


def test_reconnect_failed_switch_rolls_back_and_sanitizes(tmp_path, monkeypatch):
    _, ref = existing(tmp_path, "old")
    paths, _ = existing(tmp_path, "found", root_id=ref.root_id)
    store = MemoryDataLocationStore()
    select(store, ref)
    original = store.begin_current_switch

    def fail(candidate):
        original(candidate)
        raise OSError("private registry details")

    monkeypatch.setattr(store, "begin_current_switch", fail)
    with pytest.raises(DataLocationError, match="registry_failed") as caught:
        make_controller(tmp_path, store).reconnect_existing(paths.data_root)
    assert "private" not in repr(caught.value)
    assert store.read_current() == ref


def test_status_categories_recovery_delete_and_success(tmp_path):
    paths, ref = existing(tmp_path)
    paths.logs_dir.mkdir()
    (paths.logs_dir / "app.log").write_bytes(b"log")
    paths.webview_storage_dir.mkdir()
    (paths.webview_storage_dir / "cache").write_bytes(b"cache")
    paths.backups_dir.mkdir()
    (paths.backups_dir / "regular.zip").write_bytes(b"backup")
    store = MemoryDataLocationStore()
    select(store, ref)
    controller = make_controller(tmp_path, store)
    snapshot = RecoverySnapshotService().create(paths, paths)
    status = controller.status()
    assert status.current_path == ref.root
    assert status.volume == ref.root.anchor
    assert status.usage["logs"] == 3
    assert status.usage["webview"] == 5
    assert status.usage["backups"] == 6
    assert status.usage["recovery"] == snapshot.size_bytes
    assert status.usage["total"] == sum(
        p.stat().st_size for p in ref.root.rglob("*") if p.is_file()
    )
    assert controller.record_success().successful_starts == 1
    freed_bytes = controller.status().recovery.size_bytes
    assert controller.delete_recovery() == freed_bytes
    assert controller.delete_recovery() == 0
    assert controller.status().recovery is None
    assert (paths.backups_dir / "regular.zip").read_bytes() == b"backup"


def test_schedule_validates_target_and_blocks_cleanup_pending(tmp_path):
    _, ref = existing(tmp_path)
    store = MemoryDataLocationStore()
    select(store, ref)
    controller = make_controller(tmp_path, store)
    parent = tmp_path / "target"
    parent.mkdir()
    estimate = controller.estimate(parent, create_recovery=False)
    assert estimate.target_root == parent / "EVE Income Tracker"
    assert estimate.recovery_bytes == 0
    controller.schedule_migration(parent, create_recovery=False)
    assert store.read_pending_migration().create_recovery is False
    with pytest.raises(DataLocationError, match="migration_pending"):
        controller.schedule_migration(parent)
    store.clear_pending_migration()
    store.write_pending_cleanup(PendingCleanup(ref))
    with pytest.raises(DataLocationError, match="cleanup_pending"):
        controller.schedule_migration(parent)
    assert store.read_pending_migration() is None


def test_fresh_start_has_no_pending_work_and_never_opens_database(
    tmp_path, monkeypatch
):
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: pytest.fail("database opened")
    )
    ref = controller.resolve_startup(
        tmp_path / "local/EVE Income Tracker", Interaction()
    )
    assert controller.execute_pending_migration() is None
    assert list(ref.root.iterdir()) == [ref.root / MARKER_NAME]


def test_legacy_confirmation_cancel_removes_only_added_marker(tmp_path):
    paths, _ = existing(tmp_path, marked=False)
    store = MemoryDataLocationStore()
    with pytest.raises(DataLocationError, match="startup_cancelled"):
        make_controller(tmp_path, store).resolve_startup(
            paths.data_root, Interaction(confirm=False)
        )
    assert not (paths.data_root / MARKER_NAME).exists()
    assert paths.database_path.is_file()
    assert paths.key_path.read_bytes() == b"protected-key"
    assert store.backend.values == {}


def test_fresh_failed_registry_write_leaves_no_half_root(tmp_path, monkeypatch):
    store = MemoryDataLocationStore()
    controller = make_controller(tmp_path, store)
    real_begin = store.begin_current_switch

    def fail(reference):
        real_begin(reference)
        raise OSError("secret registry details")

    monkeypatch.setattr(store, "begin_current_switch", fail)
    with pytest.raises(DataLocationError, match="registry_failed"):
        controller.resolve_startup(tmp_path / "local/EVE Income Tracker", Interaction())
    assert list((tmp_path / "local").iterdir()) == []
    assert store.backend.values == {}


def test_committed_switch_cleanup_failure_keeps_selected_data(tmp_path, monkeypatch):
    from app.data_location_store import PREVIOUS_DATA_ROOT

    _, ref = existing(tmp_path, "old")
    paths, candidate = existing(tmp_path, "found", root_id=ref.root_id)
    store = MemoryDataLocationStore()
    select(store, ref)
    original = store.backend.delete

    def fail(name):
        if name == PREVIOUS_DATA_ROOT and name in store.backend.values:
            raise OSError("secret registry cleanup error")
        original(name)

    monkeypatch.setattr(store.backend, "delete", fail)
    with pytest.raises(DataLocationError, match="registry_finalize_failed"):
        make_controller(tmp_path, store).reconnect_existing(paths.data_root)
    assert store.read_current() == candidate
    assert read_data_root_marker(candidate.root).state == "ready"
    assert paths.database_path.is_file()


def test_startup_recovers_interrupted_switch_before_validation(tmp_path):
    _, ref = existing(tmp_path, "old")
    _, candidate = existing(tmp_path, "found", root_id=ref.root_id)
    store = MemoryDataLocationStore()
    select(store, ref)
    store.begin_current_switch(candidate)
    controller = make_controller(tmp_path, store)
    assert (
        controller.resolve_startup(tmp_path / "local/EVE Income Tracker", Interaction())
        == ref
    )
    assert store.read_current() == ref
    assert not any(name.startswith("Previous") for name in store.backend.values)


def test_reparse_root_is_rejected_without_following_it(tmp_path):
    import os
    import subprocess

    paths, ref = existing(tmp_path, "real")
    link_parent = tmp_path / "alias"
    if os.name == "nt":
        subprocess.run(
            [
                "cmd",
                "/c",
                "mklink",
                "/J",
                str(link_parent),
                str(paths.data_root.parent),
            ],
            check=True,
            capture_output=True,
        )
    else:
        link_parent.symlink_to(paths.data_root.parent, target_is_directory=True)
    try:
        store = MemoryDataLocationStore()
        select(store, ref)
        with pytest.raises(DataLocationError, match="path_reparse"):
            make_controller(tmp_path, store).reconnect_existing(
                link_parent / "EVE Income Tracker"
            )
        assert store.read_current() == ref
        assert paths.key_path.read_bytes() == b"protected-key"
    finally:
        if os.name == "nt":
            link_parent.rmdir()
        else:
            link_parent.unlink()


def test_pending_cleanup_is_retried_before_noop_and_displayed(tmp_path):
    _, current = existing(tmp_path, "current")
    old_paths, old = existing(tmp_path, "old")
    store = MemoryDataLocationStore()
    select(store, current)
    store.write_pending_cleanup(PendingCleanup(old))
    controller = make_controller(tmp_path, store)
    assert controller.status().pending_cleanup == PendingCleanup(old)
    assert controller.execute_pending_migration() is None
    assert not old_paths.data_root.exists()
    assert store.read_pending_cleanup() is None
    assert store.read_current() == current


def test_failed_pending_migration_keeps_original_and_request(tmp_path):
    paths, current = existing(tmp_path)
    parent = tmp_path / "target"
    parent.mkdir()
    store = MemoryDataLocationStore()
    select(store, current)
    controller = make_controller(tmp_path, store)
    controller.schedule_migration(parent)
    target = parent / "EVE Income Tracker"
    target.mkdir()
    (target / "foreign").write_bytes(b"keep")
    with pytest.raises(DataLocationError, match="target_not_empty"):
        controller.execute_pending_migration()
    assert store.read_current() == current
    assert paths.key_path.read_bytes() == b"protected-key"
    assert store.read_pending_migration().target_parent == parent
    assert (target / "foreign").read_bytes() == b"keep"
