from __future__ import annotations

import importlib
import importlib.util
import os
from contextlib import nullcontext
from uuid import uuid4

import pytest

from app import data_location_store, data_roots
from app.data_location_store import (
    CURRENT_SWITCH_COMMITTED,
    DATA_ROOT,
    DATA_ROOT_ID,
    DATA_ROOT_SCHEMA,
    MIGRATION_TRANSACTION,
    PENDING_CLEANUP_QUARANTINE,
    PENDING_CLEANUP_ROOT,
    PENDING_CLEANUP_ROOT_ID,
    PENDING_MIGRATION_CREATE_RECOVERY,
    PENDING_MIGRATION_TARGET,
    PREVIOUS_DATA_ROOT,
    PREVIOUS_DATA_ROOT_ABSENT,
    PREVIOUS_DATA_ROOT_ID,
    PREVIOUS_DATA_ROOT_SCHEMA,
    REG_DWORD,
    REG_SZ,
    MemoryDataLocationStore,
    PendingCleanup,
    WindowsDataLocationStore,
)
from app.data_roots import DataRootMarker, DataRootReference, write_data_root_marker


def cleanup_function():
    assert importlib.util.find_spec("app.uninstall_cleanup") is not None
    return importlib.import_module("app.uninstall_cleanup").cleanup_uninstall_data


def prepared_root(tmp_path):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    reference = DataRootReference(root, str(uuid4()), 1)
    write_data_root_marker(
        root, DataRootMarker(reference, "2026-10-02T00:00:00Z", "ready")
    )
    (root / "wallet.txt").write_text("keep", encoding="utf-8")
    store = MemoryDataLocationStore()
    store.backend.write(DATA_ROOT, str(root), REG_SZ)
    store.backend.write(DATA_ROOT_ID, reference.root_id, REG_SZ)
    store.backend.write(DATA_ROOT_SCHEMA, 1, REG_DWORD)
    return root, reference, store


def test_uninstall_helper_deletes_only_owned_root_and_leaves_registry_for_inno(
    tmp_path,
):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    before = dict(store.backend.values)

    result = cleanup(str(root), reference.root_id, registry_backend=store.backend)

    assert result == 0
    assert not root.exists()
    assert all(store.backend.values[name] == value for name, value in before.items())
    pending = store.read_pending_cleanup()
    assert pending.reference == reference
    assert pending.removal is not None
    assert not pending.removal.quarantine.exists()


def test_uninstall_helper_accepts_production_hex_uuid_v4(tmp_path):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    hex_id = reference.root_id.replace("-", "")
    assert len(hex_id) == 32
    write_data_root_marker(
        root,
        DataRootMarker(
            DataRootReference(root, hex_id, 1), "2026-10-02T00:00:00Z", "ready"
        ),
    )
    store.backend.write(DATA_ROOT_ID, hex_id, REG_SZ)

    assert cleanup(str(root), hex_id, registry_backend=store.backend) == 0
    assert not root.exists()


def test_uninstall_helper_does_not_equate_uuid_spellings(tmp_path):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    hex_id = reference.root_id.replace("-", "")
    store.backend.write(DATA_ROOT_ID, hex_id, REG_SZ)
    before = dict(store.backend.values)

    assert cleanup(str(root), hex_id, registry_backend=store.backend) == 2
    assert (root / "wallet.txt").read_text(encoding="utf-8") == "keep"
    assert store.backend.values == before


@pytest.mark.parametrize(
    ("name", "value", "kind"),
    [
        (PREVIOUS_DATA_ROOT, "residue", REG_SZ),
        (PREVIOUS_DATA_ROOT_ID, "residue", REG_SZ),
        (PREVIOUS_DATA_ROOT_SCHEMA, 1, REG_DWORD),
        (PREVIOUS_DATA_ROOT_ABSENT, 1, REG_DWORD),
        (CURRENT_SWITCH_COMMITTED, 1, REG_DWORD),
        (MIGRATION_TRANSACTION, "{}", REG_SZ),
        (PENDING_CLEANUP_ROOT, "residue", REG_SZ),
        (PENDING_CLEANUP_ROOT_ID, "residue", REG_SZ),
        (PENDING_CLEANUP_QUARANTINE, "residue", REG_SZ),
        (PENDING_MIGRATION_TARGET, "residue", REG_SZ),
        (PENDING_MIGRATION_CREATE_RECOVERY, 1, REG_DWORD),
        (DATA_ROOT_SCHEMA, 2, REG_DWORD),
        (DATA_ROOT, "", REG_SZ),
        (DATA_ROOT_ID, "wrong", REG_SZ),
        (DATA_ROOT, "wrong", REG_DWORD),
    ],
)
def test_uninstall_helper_rejects_stale_or_malformed_registry(
    tmp_path, name, value, kind
):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    store.backend.write(name, value, kind)
    before = dict(store.backend.values)

    result = cleanup(str(root), reference.root_id, registry_backend=store.backend)

    assert result != 0
    assert (root / "wallet.txt").read_text(encoding="utf-8") == "keep"
    assert store.backend.values == before


def test_uninstall_helper_rejects_partial_current_record(tmp_path):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    store.backend.delete(DATA_ROOT_ID)
    before = dict(store.backend.values)

    assert cleanup(str(root), reference.root_id, registry_backend=store.backend) != 0
    assert (root / "wallet.txt").read_text(encoding="utf-8") == "keep"
    assert store.backend.values == before


def test_uninstall_helper_rejects_marker_mismatch_and_preserves_registry(tmp_path):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    marker_path = root / data_roots.MARKER_NAME
    marker_path.write_text('{"app_id":"spoofed"}', encoding="utf-8")
    before = dict(store.backend.values)

    assert cleanup(str(root), reference.root_id, registry_backend=store.backend) != 0
    assert (root / "wallet.txt").read_text(encoding="utf-8") == "keep"
    assert store.backend.values == before


def test_uninstall_helper_preserves_plain_directory_replacement_at_delete_boundary(
    tmp_path, monkeypatch
):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    moved_original = tmp_path / "moved-original"
    original_scan = data_roots._reject_reparse_tree

    def swap_after_scan(path):
        original_scan(path)
        if path.name.startswith(".eve-income-tracker-removing-"):
            os.replace(path, moved_original)
            path.mkdir()
            (path / "replacement.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(data_roots, "_reject_reparse_tree", swap_after_scan)
    before = dict(store.backend.values)

    result = cleanup(str(root), reference.root_id, registry_backend=store.backend)

    assert result != 0
    assert (moved_original / "wallet.txt").read_text(encoding="utf-8") == "keep"
    replacements = list(tmp_path.rglob("replacement.txt"))
    assert len(replacements) == 1
    assert replacements[0].read_text(encoding="utf-8") == "replacement"
    assert all(store.backend.values[name] == value for name, value in before.items())
    assert store.read_pending_cleanup().removal is not None


class SimulatedCrash(BaseException):
    pass


class FakeDurableWinreg:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1
    KEY_WRITE = 0x20006

    def __init__(self, values):
        self.values = dict(values)
        self.flushed = None
        self.fail_flush = False
        self.events = []

    def OpenKey(self, *args):
        return nullcontext(self)

    def CreateKeyEx(self, key, subkey, reserved=0, access=KEY_WRITE):
        self.write_access = access
        return nullcontext(self)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        assert key is self
        self.events.append(("set", name))
        self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        self.values.pop(name, None)

    def FlushKey(self, key):
        assert key is self
        self.events.append(("flush",))
        if not self.write_access & self.KEY_QUERY_VALUE:
            raise PermissionError("FlushKey requires KEY_QUERY_VALUE")
        if self.fail_flush:
            raise OSError("injected registry flush failure")
        self.flushed = dict(self.values)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("fail_flush", [False, True])
def test_uninstall_flushes_exact_receipt_before_rename(
    tmp_path, monkeypatch, resume, fail_flush
):
    root, reference, store = prepared_root(tmp_path)
    if resume:
        receipt = data_roots.prepare_owned_data_root_removal(reference)
        store.write_pending_cleanup(PendingCleanup(reference, receipt))
    native = FakeDurableWinreg(store.backend.values)
    native.fail_flush = fail_flush
    backend = object.__new__(data_location_store._WindowsRegistryBackend)
    backend.winreg = native
    real_rename = data_roots.os.rename

    def rename_after_flush(source, target):
        assert native.flushed == native.values
        assert native.events[-2:] == [("set", PENDING_CLEANUP_QUARANTINE), ("flush",)]
        pending = WindowsDataLocationStore(backend).read_pending_cleanup()
        assert pending.removal.quarantine == target
        real_rename(source, target)

    monkeypatch.setattr(data_roots.os, "rename", rename_after_flush)
    result = cleanup_function()(str(root), reference.root_id, registry_backend=backend)
    if fail_flush:
        assert result == 2
        assert (root / "wallet.txt").read_text(encoding="utf-8") == "keep"
        pending = WindowsDataLocationStore(backend).read_pending_cleanup()
        assert not pending.removal.quarantine.exists()
        journal = dict(native.values)
        native.fail_flush = False
        assert (
            cleanup_function()(str(root), reference.root_id, registry_backend=backend)
            == 0
        )
        assert native.values == journal
    else:
        assert result == 0
    assert not root.exists()
    assert native.flushed == native.values


def test_windows_backend_does_not_flush_unrelated_registry_writes():
    native = FakeDurableWinreg({})
    backend = object.__new__(data_location_store._WindowsRegistryBackend)
    backend.winreg = native
    backend.write(DATA_ROOT, "selected", REG_SZ)
    assert native.values == {DATA_ROOT: ("selected", REG_SZ)}
    assert native.events == [("set", DATA_ROOT)]


@pytest.mark.parametrize(
    "boundary", ["receipt", "rename", "partial", "marker", "complete"]
)
def test_uninstall_resumes_exact_durable_receipt_after_crash(
    tmp_path, monkeypatch, boundary
):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    original = {
        name: store.backend.read(name)
        for name in (DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
    }
    real_write = WindowsDataLocationStore.write_pending_cleanup
    real_rename = data_roots.os.rename
    real_delete = data_roots.delete_tree_bound

    def write_then_crash(self, pending):
        real_write(self, pending)
        raise SimulatedCrash

    def rename_then_crash(source, target):
        assert (
            WindowsDataLocationStore(store.backend)
            .read_pending_cleanup()
            .removal.quarantine
            == target
        )
        real_rename(source, target)
        raise SimulatedCrash

    def delete_then_crash(path, identity, *, marker_last=None):
        pending = WindowsDataLocationStore(store.backend).read_pending_cleanup()
        assert pending.removal.quarantine == path
        if boundary == "complete":
            real_delete(path, identity, marker_last=marker_last)
        else:
            (path / "wallet.txt").unlink()
            if boundary == "marker":
                (path / data_roots.MARKER_NAME).unlink()
        raise SimulatedCrash

    with monkeypatch.context() as crash:
        if boundary == "receipt":
            crash.setattr(
                WindowsDataLocationStore, "write_pending_cleanup", write_then_crash
            )
        elif boundary == "rename":
            crash.setattr(data_roots.os, "rename", rename_then_crash)
        else:
            crash.setattr(data_roots, "delete_tree_bound", delete_then_crash)
        with pytest.raises(SimulatedCrash):
            cleanup(str(root), reference.root_id, registry_backend=store.backend)

    pending = WindowsDataLocationStore(store.backend).read_pending_cleanup()
    assert pending is not None and pending.removal is not None
    assert pending.reference == reference
    journal = dict(store.backend.values)
    for _ in range(2):
        assert (
            cleanup(str(root), reference.root_id, registry_backend=store.backend) == 0
        )
        assert not os.path.lexists(root)
        assert not os.path.lexists(pending.removal.quarantine)
        assert store.backend.values == journal
        assert all(
            store.backend.read(name) == value for name, value in original.items()
        )


@pytest.mark.parametrize(
    "damage",
    [
        "foreign-original",
        "foreign-quarantine",
        "late-child",
        "missing-receipt",
        "mismatch",
        "malformed",
    ],
)
def test_uninstall_rejects_unsafe_durable_cleanup_without_mutation(tmp_path, damage):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    receipt = data_roots.prepare_owned_data_root_removal(reference)
    store.write_pending_cleanup(PendingCleanup(reference, receipt))
    os.rename(root, receipt.quarantine)
    if damage == "foreign-original":
        root.mkdir()
        (root / "foreign.txt").write_bytes(b"foreign")
    elif damage == "foreign-quarantine":
        receipt.quarantine.rename(tmp_path / "displaced")
        receipt.quarantine.mkdir()
        (receipt.quarantine / "foreign.txt").write_bytes(b"foreign")
    elif damage == "late-child":
        (receipt.quarantine / "wallet.txt").unlink()
        (receipt.quarantine / data_roots.MARKER_NAME).unlink()
        (receipt.quarantine / "foreign.txt").write_bytes(b"foreign")
    elif damage == "missing-receipt":
        store.backend.delete(PENDING_CLEANUP_QUARANTINE)
    elif damage == "mismatch":
        store.backend.write(PENDING_CLEANUP_ROOT_ID, str(uuid4()), REG_SZ)
    else:
        store.backend.write(PENDING_CLEANUP_QUARANTINE, "{}", REG_SZ)
    before = dict(store.backend.values)
    files = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert cleanup(str(root), reference.root_id, registry_backend=store.backend) == 2
    assert store.backend.values == before
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == files


@pytest.mark.parametrize("stage", ["prepare", "persist", "delete"])
@pytest.mark.parametrize(
    "changed", [DATA_ROOT_ID, PENDING_MIGRATION_TARGET, PENDING_CLEANUP_ROOT_ID]
)
def test_uninstall_checks_registry_before_deletion_and_before_success(
    tmp_path, monkeypatch, stage, changed
):
    cleanup = cleanup_function()
    module = importlib.import_module("app.uninstall_cleanup")
    root, reference, store = prepared_root(tmp_path)

    def mutate():
        store.backend.write(changed, "foreign-state", REG_SZ)

    if stage == "prepare":
        real_prepare = module.prepare_owned_data_root_removal

        def prepare(ref):
            receipt = real_prepare(ref)
            mutate()
            return receipt

        monkeypatch.setattr(module, "prepare_owned_data_root_removal", prepare)
    elif stage == "persist":
        real_write = WindowsDataLocationStore.write_pending_cleanup

        def persist(self, pending):
            real_write(self, pending)
            mutate()

        monkeypatch.setattr(WindowsDataLocationStore, "write_pending_cleanup", persist)
    else:
        real_resume = module.resume_owned_data_root_removal

        def resume(receipt):
            real_resume(receipt)
            mutate()

        monkeypatch.setattr(module, "resume_owned_data_root_removal", resume)
    assert cleanup(str(root), reference.root_id, registry_backend=store.backend) == 2
    assert store.backend.read(changed) == ("foreign-state", REG_SZ)
    if stage != "delete":
        assert (root / "wallet.txt").read_text(encoding="utf-8") == "keep"
