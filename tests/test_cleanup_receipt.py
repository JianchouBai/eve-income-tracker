"""Crash-safe removal of a committed migration source."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app import data_roots, windows_owned_delete
from app.data_location_controller import DataLocationStatus
from app.data_location_store import (
    PENDING_CLEANUP_QUARANTINE,
    PENDING_CLEANUP_ROOT,
    PENDING_CLEANUP_ROOT_ID,
    REG_SZ,
    DataLocationStoreError,
    PendingCleanup,
    WindowsDataLocationStore,
)
from app.data_migration import DataMigrationService
from app.data_roots import (
    MARKER_NAME,
    DataRootMarker,
    DataRootReference,
    DataRootRemovalReceipt,
    DataRootValidationError,
    prepare_owned_data_root_removal,
    resume_owned_data_root_removal,
    write_data_root_marker,
)


class SimulatedCrash(BaseException):
    pass


class Backend:
    def __init__(self):
        self.values = {}
        self.operations = []
        self.crash_after = None

    def read(self, name):
        return self.values.get(name)

    def write(self, name, value, kind):
        self.values[name] = (value, kind)
        self.operations.append(("write", name))
        if len(self.operations) == self.crash_after:
            raise SimulatedCrash

    def delete(self, name):
        self.values.pop(name, None)
        self.operations.append(("delete", name))
        if len(self.operations) == self.crash_after:
            raise SimulatedCrash


def owned(tmp_path):
    root = tmp_path / "volume" / "EVE Income Tracker"
    root.mkdir(parents=True)
    reference = DataRootReference(root, uuid4().hex, 1)
    marker = DataRootMarker(reference, "2026-10-04T00:00:00Z", "ready")
    write_data_root_marker(root, marker)
    (root / "private.txt").write_bytes(b"private")
    return reference, marker


def test_receipt_roundtrip_and_publication_order(tmp_path):
    reference, marker = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    assert receipt.reference == reference
    assert receipt.marker == marker
    assert receipt.quarantine.parent == reference.root.parent
    assert receipt.quarantine.name.startswith(".eve-income-tracker-removing-")
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    store.write_pending_cleanup(PendingCleanup(reference, receipt))
    assert backend.operations == [
        ("delete", PENDING_CLEANUP_QUARANTINE),
        ("write", PENDING_CLEANUP_ROOT_ID),
        ("write", PENDING_CLEANUP_ROOT),
        ("write", PENDING_CLEANUP_QUARANTINE),
    ]
    assert WindowsDataLocationStore(backend).read_pending_cleanup() == PendingCleanup(
        reference, receipt
    )
    store.clear_pending_cleanup()
    assert backend.operations[-3:] == [
        ("delete", PENDING_CLEANUP_ROOT),
        ("delete", PENDING_CLEANUP_ROOT_ID),
        ("delete", PENDING_CLEANUP_QUARANTINE),
    ]


@pytest.mark.parametrize("boundary", [1, 2, 3, 4])
def test_receipt_publication_crash_boundaries(tmp_path, boundary):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    backend = Backend()
    backend.crash_after = boundary
    with pytest.raises(SimulatedCrash):
        WindowsDataLocationStore(backend).write_pending_cleanup(
            PendingCleanup(reference, receipt)
        )
    observed = WindowsDataLocationStore(backend).read_pending_cleanup()
    expected = (
        None
        if boundary < 3
        else PendingCleanup(reference, receipt if boundary == 4 else None)
    )
    assert observed == expected
    assert reference.root.exists()


@pytest.mark.parametrize("boundary", [1, 2, 3])
def test_receipt_clear_crash_boundaries_and_inert_residue(tmp_path, boundary):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    store.write_pending_cleanup(PendingCleanup(reference, receipt))
    backend.operations.clear()
    backend.crash_after = boundary
    with pytest.raises(SimulatedCrash):
        store.clear_pending_cleanup()
    assert WindowsDataLocationStore(backend).read_pending_cleanup() is None
    backend.crash_after = None
    other = DataRootReference(tmp_path / "other" / "EVE Income Tracker", uuid4().hex, 1)
    store.write_pending_cleanup(PendingCleanup(other))
    assert store.read_pending_cleanup() == PendingCleanup(other)
    assert PENDING_CLEANUP_QUARANTINE not in backend.values


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(version=2),
        lambda p: p.update(quarantine="relative"),
        lambda p: p.update(quarantine=str(Path("/tmp/wrong"))),
        lambda p: p.update(quarantine=p["quarantine"] + "/."),
        lambda p: p.update(identity=[True, 7]),
        lambda p: p.update(identity=[1, 0]),
        lambda p: p["marker"].update(state="incomplete"),
        lambda p: p["marker"].update(schema=True),
        lambda p: p["marker"].update(root=p["marker"]["root"] + "/."),
        lambda p: p["reference"].update(root=p["reference"]["root"] + "/."),
        lambda p: p["reference"].update(root_id="wrong"),
        lambda p: p.update(extra="unexpected"),
    ],
)
def test_receipt_json_strictly_rejects_malformed_fields(tmp_path, change):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    store.write_pending_cleanup(PendingCleanup(reference, receipt))
    payload = json.loads(backend.values[PENDING_CLEANUP_QUARANTINE][0])
    change(payload)
    backend.values[PENDING_CLEANUP_QUARANTINE] = (json.dumps(payload), REG_SZ)
    with pytest.raises(DataLocationStoreError, match="registry_invalid"):
        store.read_pending_cleanup()


def test_receipt_rejects_wrong_sibling_and_identity(tmp_path):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    bad = DataRootRemovalReceipt(
        reference,
        tmp_path / "other" / receipt.quarantine.name,
        receipt.identity,
        receipt.marker,
    )
    with pytest.raises(DataRootValidationError):
        resume_owned_data_root_removal(bad)
    assert (reference.root / "private.txt").read_bytes() == b"private"


def test_receipt_structural_validation_rejects_nul_in_parent(tmp_path):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    bad_root = Path(str(reference.root.parent) + chr(0)) / reference.root.name
    bad_reference = DataRootReference(bad_root, reference.root_id, 1)
    bad_receipt = DataRootRemovalReceipt(
        bad_reference,
        bad_root.with_name(receipt.quarantine.name),
        receipt.identity,
        DataRootMarker(bad_reference, receipt.marker.created_at, "ready"),
    )
    with pytest.raises(DataRootValidationError):
        data_roots.validate_removal_receipt(bad_receipt)


def test_resume_rejects_newly_protected_home_parent(tmp_path, monkeypatch):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: reference.root.parent))
    with pytest.raises(DataRootValidationError):
        resume_owned_data_root_removal(receipt)
    assert (reference.root / "private.txt").read_bytes() == b"private"


def test_both_owned_locations_are_ambiguous_and_retained(tmp_path):
    reference, marker = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    os.rename(reference.root, receipt.quarantine)
    reference.root.mkdir()
    write_data_root_marker(reference.root, marker)
    (reference.root / "new-private").write_bytes(b"new")
    with pytest.raises(DataRootValidationError):
        resume_owned_data_root_removal(receipt)
    assert (receipt.quarantine / "private.txt").read_bytes() == b"private"
    assert (reference.root / "new-private").read_bytes() == b"new"


def test_recreated_owned_original_with_changed_timestamp_is_ambiguous(tmp_path):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    os.rename(reference.root, receipt.quarantine)
    reference.root.mkdir()
    write_data_root_marker(
        reference.root,
        DataRootMarker(reference, "2026-10-05T00:00:00Z", "ready"),
    )
    with pytest.raises(DataRootValidationError):
        resume_owned_data_root_removal(receipt)
    assert (receipt.quarantine / "private.txt").read_bytes() == b"private"
    assert (reference.root / MARKER_NAME).exists()


def test_foreign_file_at_original_does_not_block_exact_quarantine_cleanup(
    tmp_path, monkeypatch
):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    os.rename(reference.root, receipt.quarantine)
    reference.root.write_bytes(b"foreign file")
    monkeypatch.setattr(data_roots, "delete_tree_bound", deleting_tree)
    resume_owned_data_root_removal(receipt)
    assert reference.root.read_bytes() == b"foreign file"
    assert not receipt.quarantine.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle deletion only")
def test_windows_handle_deletion_opens_marker_after_other_children(
    tmp_path, monkeypatch
):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    os.rename(reference.root, receipt.quarantine)
    marker_path = receipt.quarantine / MARKER_NAME
    private_path = receipt.quarantine / "private.txt"
    real_open = windows_owned_delete._open_bound_handle
    marker_opened = []

    def observe(kernel, path, *, delete=True):
        if path == marker_path:
            marker_opened.append(True)
            assert not private_path.exists()
        return real_open(kernel, path, delete=delete)

    monkeypatch.setattr(windows_owned_delete, "_open_bound_handle", observe)
    resume_owned_data_root_removal(receipt)
    assert marker_opened == [True]


def test_legacy_cleanup_roundtrips_without_receipt(tmp_path):
    reference, _ = owned(tmp_path)
    store = WindowsDataLocationStore(Backend())
    store.write_pending_cleanup(PendingCleanup(reference))
    assert store.read_pending_cleanup() == PendingCleanup(reference)
    assert PENDING_CLEANUP_QUARANTINE not in store.backend.values


def test_status_reports_exact_quarantine_without_private_marker_details(tmp_path):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    status = DataLocationStatus(
        tmp_path / "selected", "F:", {}, None, PendingCleanup(reference, receipt)
    )
    assert status.pending_cleanup_path == receipt.quarantine
    assert reference.root_id not in str(status.pending_cleanup_path)
    assert status.pending_cleanup_path != reference.root


def selected_service(tmp_path, backend, original):
    selected = tmp_path / "selected" / "EVE Income Tracker"
    if not selected.exists():
        selected.mkdir(parents=True)
        selected_ref = DataRootReference(selected, uuid4().hex, 1)
        write_data_root_marker(
            selected, DataRootMarker(selected_ref, "2026-10-04T00:00:00Z", "ready")
        )
        WindowsDataLocationStore(backend).begin_current_switch(selected_ref)
        WindowsDataLocationStore(backend).finalize_current_switch()
    store = WindowsDataLocationStore(backend)
    service = DataMigrationService(
        store,
        resource_root=tmp_path / "program",
        destination_validator=lambda paths: None,
    )
    return store, service


def deleting_tree(root, identity, *, marker_last=None, crash_after=None):
    assert data_roots._directory_identity(root) == identity
    assert marker_last == MARKER_NAME or (
        marker_last is None and list(root.iterdir()) == []
    )
    operations = 0

    def remove(path):
        nonlocal operations
        children = sorted(path.iterdir(), key=lambda child: child.name == MARKER_NAME)
        for child in children:
            if child.is_dir():
                remove(child)
            else:
                child.unlink()
                operations += 1
                if operations == crash_after:
                    raise SimulatedCrash
        path.rmdir()
        operations += 1
        if operations == crash_after:
            raise SimulatedCrash

    remove(root)


@pytest.mark.parametrize("crash_after", range(1, 6))
def test_fresh_instance_converges_after_each_delete_operation(
    tmp_path, monkeypatch, crash_after
):
    reference, _ = owned(tmp_path)
    nested = reference.root / "nested"
    nested.mkdir()
    (nested / "child").write_bytes(b"child")
    backend = Backend()
    store, service = selected_service(tmp_path, backend, reference)
    store.write_pending_cleanup(PendingCleanup(reference))

    def crash_delete(root, identity, *, marker_last=None):
        deleting_tree(root, identity, marker_last=marker_last, crash_after=crash_after)

    monkeypatch.setattr(data_roots, "delete_tree_bound", crash_delete)
    with pytest.raises(SimulatedCrash):
        service.retry_pending_cleanup()
    pending = WindowsDataLocationStore(backend).read_pending_cleanup()
    assert pending.removal is not None
    assert pending.removal.quarantine.parent == reference.root.parent
    monkeypatch.setattr(data_roots, "delete_tree_bound", deleting_tree)
    for _ in range(2):
        restarted_store, restarted = selected_service(tmp_path, backend, reference)
        assert restarted.retry_pending_cleanup()
        assert restarted_store.read_pending_cleanup() is None
    assert not reference.root.exists()
    assert not pending.removal.quarantine.exists()


def test_fresh_instance_recovers_after_receipt_write_and_rename(tmp_path, monkeypatch):
    reference, _ = owned(tmp_path)
    backend = Backend()
    store, service = selected_service(tmp_path, backend, reference)
    store.write_pending_cleanup(PendingCleanup(reference))
    real_rename = os.rename

    def rename_then_die(source, destination):
        real_rename(source, destination)
        raise SimulatedCrash

    monkeypatch.setattr(data_roots.os, "rename", rename_then_die)
    with pytest.raises(SimulatedCrash):
        service.retry_pending_cleanup()
    pending = WindowsDataLocationStore(backend).read_pending_cleanup()
    assert pending.removal is not None
    assert pending.removal.quarantine.exists()
    monkeypatch.setattr(data_roots.os, "rename", real_rename)
    monkeypatch.setattr(data_roots, "delete_tree_bound", deleting_tree)
    _, restarted = selected_service(tmp_path, backend, reference)
    assert restarted.retry_pending_cleanup()
    assert store.read_pending_cleanup() is None


def test_legacy_missing_root_stays_pending_as_ambiguous(tmp_path):
    reference, _ = owned(tmp_path)
    backend = Backend()
    store, service = selected_service(tmp_path, backend, reference)
    store.write_pending_cleanup(PendingCleanup(reference))
    reference.root.rename(reference.root.with_name("unknown-displacement"))
    assert service.retry_pending_cleanup() is False
    assert store.read_pending_cleanup() == PendingCleanup(reference)


def test_markerless_empty_quarantine_uses_identity_bound_root_delete(
    tmp_path, monkeypatch
):
    reference, _ = owned(tmp_path)
    receipt = prepare_owned_data_root_removal(reference)
    os.rename(reference.root, receipt.quarantine)
    (receipt.quarantine / "private.txt").unlink()
    (receipt.quarantine / MARKER_NAME).unlink()
    observed = []

    def delete_empty(root, identity, *, marker_last=None):
        observed.append((root, identity, marker_last))
        assert list(root.iterdir()) == []
        root.rmdir()

    monkeypatch.setattr(data_roots, "delete_tree_bound", delete_empty)
    resume_owned_data_root_removal(receipt)
    assert observed == [(receipt.quarantine, receipt.identity, None)]


def test_fresh_instance_recovers_after_receipt_persisted_before_rename(
    tmp_path, monkeypatch
):
    reference, _ = owned(tmp_path)
    backend = Backend()
    store, service = selected_service(tmp_path, backend, reference)
    store.write_pending_cleanup(PendingCleanup(reference))
    real_rename = data_roots.os.rename

    def die_before_rename(source, destination):
        raise SimulatedCrash

    monkeypatch.setattr(data_roots.os, "rename", die_before_rename)
    with pytest.raises(SimulatedCrash):
        service.retry_pending_cleanup()
    pending = store.read_pending_cleanup()
    assert pending.removal is not None
    assert reference.root.exists()
    monkeypatch.setattr(data_roots.os, "rename", real_rename)
    monkeypatch.setattr(data_roots, "delete_tree_bound", deleting_tree)
    _, restarted = selected_service(tmp_path, backend, reference)
    assert restarted.retry_pending_cleanup()
    assert store.read_pending_cleanup() is None


@pytest.mark.parametrize("boundary", [1, 2, 3])
def test_fresh_instance_converges_after_each_registry_clear(
    tmp_path, monkeypatch, boundary
):
    reference, _ = owned(tmp_path)
    backend = Backend()
    store, service = selected_service(tmp_path, backend, reference)
    receipt = prepare_owned_data_root_removal(reference)
    store.write_pending_cleanup(PendingCleanup(reference, receipt))
    os.rename(reference.root, receipt.quarantine)
    monkeypatch.setattr(data_roots, "delete_tree_bound", deleting_tree)
    backend.operations.clear()
    backend.crash_after = boundary
    with pytest.raises(SimulatedCrash):
        service.retry_pending_cleanup()
    assert not receipt.quarantine.exists()
    backend.crash_after = None
    for _ in range(2):
        restarted_store, restarted = selected_service(tmp_path, backend, reference)
        assert restarted.retry_pending_cleanup()
        assert restarted_store.read_pending_cleanup() is None
    assert PENDING_CLEANUP_QUARANTINE not in backend.values


@pytest.mark.parametrize(
    "damage", ["wrong-identity", "marker", "foreign-original", "markerless-nonempty"]
)
def test_receipt_retained_and_private_bytes_survive_unsafe_states(
    tmp_path, monkeypatch, damage
):
    reference, _ = owned(tmp_path)
    backend = Backend()
    store, service = selected_service(tmp_path, backend, reference)
    receipt = prepare_owned_data_root_removal(reference)
    store.write_pending_cleanup(PendingCleanup(reference, receipt))
    os.rename(reference.root, receipt.quarantine)
    if damage == "wrong-identity":
        displaced = tmp_path / "displaced"
        os.rename(receipt.quarantine, displaced)
        receipt.quarantine.mkdir()
        (receipt.quarantine / "replacement").write_bytes(b"replacement")
    elif damage == "marker":
        (receipt.quarantine / MARKER_NAME).write_bytes(b"wrong")
    elif damage == "foreign-original":
        reference.root.mkdir()
        (reference.root / "foreign").write_bytes(b"foreign")
    else:
        (receipt.quarantine / MARKER_NAME).unlink()
    if damage == "foreign-original":
        monkeypatch.setattr(data_roots, "delete_tree_bound", deleting_tree)
        assert service.retry_pending_cleanup() is True
        assert (reference.root / "foreign").read_bytes() == b"foreign"
        return
    assert service.retry_pending_cleanup() is False
    assert store.read_pending_cleanup() == PendingCleanup(reference, receipt)
    if damage == "wrong-identity":
        assert (displaced / "private.txt").read_bytes() == b"private"
        assert (receipt.quarantine / "replacement").read_bytes() == b"replacement"
    else:
        assert (receipt.quarantine / "private.txt").read_bytes() == b"private"
