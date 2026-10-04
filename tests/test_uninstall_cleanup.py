from __future__ import annotations

import importlib
import importlib.util
import os
from uuid import uuid4

import pytest

from app import data_roots
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
    REG_DWORD,
    REG_SZ,
    MemoryDataLocationStore,
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


def test_uninstall_helper_deletes_only_owned_root_and_leaves_registry_for_inno(tmp_path):
    cleanup = cleanup_function()
    root, reference, store = prepared_root(tmp_path)
    before = dict(store.backend.values)

    result = cleanup(str(root), reference.root_id, registry_backend=store.backend)

    assert result == 0
    assert not root.exists()
    assert store.backend.values == before


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
        (MIGRATION_TRANSACTION, '{}', REG_SZ),
        (PENDING_CLEANUP_ROOT, "residue", REG_SZ),
        (PENDING_CLEANUP_ROOT_ID, "residue", REG_SZ),
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
    assert store.backend.values == before
