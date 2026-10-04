from uuid import uuid4

import pytest

from app.data_location_store import (
    DATA_ROOT,
    DATA_ROOT_ID,
    DATA_ROOT_SCHEMA,
    PENDING_CLEANUP_ROOT,
    PENDING_CLEANUP_ROOT_ID,
    PENDING_MIGRATION_TARGET,
    PREVIOUS_DATA_ROOT,
    PREVIOUS_DATA_ROOT_ID,
    PREVIOUS_DATA_ROOT_SCHEMA,
    REG_DWORD,
    REG_SZ,
    DataLocationStoreError,
    MemoryDataLocationStore,
    MigrationTransaction,
    PendingCleanup,
    PendingMigration,
    WindowsDataLocationStore,
)
from app.data_roots import DataRootReference


def ref(tmp_path, name):
    return DataRootReference(tmp_path / name / "EVE Income Tracker", str(uuid4()), 1)


class Backend:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.fail_after = None
        self.fail_delete_after = None
        self.deletes = []

    def read(self, name):
        return self.values.get(name)

    def write(self, name, value, kind):
        if self.fail_after is not None and len(self.writes) == self.fail_after:
            raise OSError("interrupted")
        self.values[name] = (value, kind)
        self.writes.append(name)

    def delete(self, name):
        if (
            self.fail_delete_after is not None
            and len(self.deletes) == self.fail_delete_after
        ):
            raise OSError("interrupted delete")
        self.values.pop(name, None)
        self.deletes.append(name)


def invalid(action):
    with pytest.raises(DataLocationStoreError) as caught:
        action()
    assert caught.value.code == str(caught.value) == "registry_invalid"
    assert "SECRET" not in repr(caught.value)


def test_round_trip(tmp_path):
    s = MemoryDataLocationStore()
    r = ref(tmp_path, "one")
    assert s.read_current() is None
    assert s.read_pending_migration() is None
    assert s.read_pending_cleanup() is None
    s.begin_current_switch(r)
    assert s.read_current() is None
    s.finalize_current_switch()
    assert s.read_current() == r
    p = PendingMigration(tmp_path / "selected", False)
    s.write_pending_migration(p)
    assert s.read_pending_migration() == p
    s.write_pending_cleanup(PendingCleanup(r))
    assert s.read_pending_cleanup() == PendingCleanup(r)
    s.clear_pending_migration()
    s.clear_pending_cleanup()
    assert s.read_pending_migration() is None
    assert s.read_pending_cleanup() is None


def test_migration_transaction_roundtrip_requires_durable_registry_value(tmp_path):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    source = ref(tmp_path, "old")
    target = ref(tmp_path, "new")
    record = MigrationTransaction(
        1,
        "7ad53c3e6fa34bd6a37a7b6ec3683f5f",
        source,
        target,
        "2026-10-04T00:00:00Z",
        None,
    )
    store.write_migration_transaction(record)
    assert backend.writes == ["MigrationTransaction"]
    assert WindowsDataLocationStore(backend).read_migration_transaction() == record
    store.clear_migration_transaction()
    assert WindowsDataLocationStore(backend).read_migration_transaction() is None


@pytest.mark.parametrize("bad_field", ["source", "target"])
@pytest.mark.parametrize("bad_value", [None, object()])
def test_public_transaction_rejects_malformed_reference_with_stable_error(
    tmp_path, bad_field, bad_value
):
    source = ref(tmp_path, "old")
    target = ref(tmp_path, "new")
    values = {"source": source, "target": target}
    values[bad_field] = bad_value
    record = MigrationTransaction(
        1,
        "7ad53c3e6fa34bd6a37a7b6ec3683f5f",
        values["source"],
        values["target"],
        "2026-10-04T00:00:00Z",
        None,
    )
    invalid(lambda: MemoryDataLocationStore().write_migration_transaction(record))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(version=True),
        lambda payload: payload.update(operation_id="not-a-uuid"),
        lambda payload: payload.update(extra="unexpected"),
        lambda payload: payload.update(staging_identity=[1]),
        lambda payload: payload["source"].update(root="relative"),
        lambda payload: payload["target"].update(schema=2),
    ],
)
def test_migration_transaction_strict_decode_rejects_malformed_registry_json(
    tmp_path, mutation
):
    import json

    source = ref(tmp_path, "old")
    target = ref(tmp_path, "new")
    payload = {
        "version": 1,
        "operation_id": "7ad53c3e6fa34bd6a37a7b6ec3683f5f",
        "source": {
            "root": str(source.root),
            "root_id": source.root_id,
            "schema": 1,
        },
        "target": {
            "root": str(target.root),
            "root_id": target.root_id,
            "schema": 1,
        },
        "created_at": "2026-10-04T00:00:00Z",
        "staging_identity": None,
    }
    mutation(payload)
    backend = Backend()
    backend.values["MigrationTransaction"] = (
        json.dumps(payload, separators=(",", ":")),
        REG_SZ,
    )

    invalid(WindowsDataLocationStore(backend).read_migration_transaction)


@pytest.mark.parametrize(
    "name,value,kind",
    [
        (DATA_ROOT, "SECRET", REG_SZ),
        (DATA_ROOT, 42, REG_DWORD),
        (DATA_ROOT, "relative", REG_SZ),
        (DATA_ROOT, "SECRET\0tail", REG_SZ),
        (DATA_ROOT_ID, "bad-id", REG_SZ),
        (DATA_ROOT_SCHEMA, 2, REG_DWORD),
        (DATA_ROOT_SCHEMA, "1", REG_SZ),
    ],
)
def test_corrupt_current(tmp_path, name, value, kind):
    b = Backend()
    r = ref(tmp_path, "one")
    b.values.update(
        {
            DATA_ROOT: (str(r.root), REG_SZ),
            DATA_ROOT_ID: (r.root_id, REG_SZ),
            DATA_ROOT_SCHEMA: (1, REG_DWORD),
        }
    )
    b.values[name] = (value, kind)
    invalid(WindowsDataLocationStore(b).read_current)


def test_corrupt_pending():
    b = Backend()
    b.values[PENDING_CLEANUP_ROOT] = ("SECRET", REG_SZ)
    invalid(WindowsDataLocationStore(b).read_pending_cleanup)
    b.values.clear()
    b.values[PENDING_MIGRATION_TARGET] = ("relative", REG_SZ)
    invalid(WindowsDataLocationStore(b).read_pending_migration)


def test_commit_order_and_finalize_validation(tmp_path):
    b = Backend()
    s = WindowsDataLocationStore(b)
    one, two = ref(tmp_path, "one"), ref(tmp_path, "two")
    s.begin_current_switch(one)
    s.finalize_current_switch()
    b.writes.clear()
    s.begin_current_switch(two)
    assert b.writes == [
        PREVIOUS_DATA_ROOT_ID,
        PREVIOUS_DATA_ROOT_SCHEMA,
        PREVIOUS_DATA_ROOT,
        DATA_ROOT_SCHEMA,
        DATA_ROOT_ID,
        DATA_ROOT,
    ]
    assert s.read_current() == one
    b.values[DATA_ROOT_ID] = ("SECRET", REG_SZ)
    invalid(s.finalize_current_switch)
    assert s.read_current() == one
    assert PREVIOUS_DATA_ROOT in b.values


@pytest.mark.parametrize("stop", range(6))
def test_interrupted_switch_recovers_previous(tmp_path, stop):
    b = Backend()
    s = WindowsDataLocationStore(b)
    one = ref(tmp_path, "one")
    s.begin_current_switch(one)
    s.finalize_current_switch()
    b.writes.clear()
    b.fail_after = stop
    with pytest.raises(OSError, match="interrupted"):
        s.begin_current_switch(ref(tmp_path, "two"))
    assert s.read_current() == one


def test_rollback_is_idempotent(tmp_path):
    s = MemoryDataLocationStore()
    one = ref(tmp_path, "one")
    s.begin_current_switch(one)
    s.finalize_current_switch()
    s.begin_current_switch(ref(tmp_path, "two"))
    s.rollback_current_switch()
    s.rollback_current_switch()
    assert s.read_current() == one


def test_initial_rollback_restores_none(tmp_path):
    s = MemoryDataLocationStore()
    s.begin_current_switch(ref(tmp_path, "one"))
    s.rollback_current_switch()
    s.rollback_current_switch()
    assert s.read_current() is None


@pytest.mark.parametrize("stop", range(5))
@pytest.mark.parametrize("retry", ["finalize", "rollback"])
def test_finalize_cleanup_failure_keeps_committed_target_and_retries(
    tmp_path, stop, retry
):
    b = Backend()
    s = WindowsDataLocationStore(b)
    first, second = ref(tmp_path, "first"), ref(tmp_path, "second")
    s.begin_current_switch(first)
    s.finalize_current_switch()
    s.begin_current_switch(second)
    b.deletes.clear()
    b.fail_delete_after = stop

    with pytest.raises(OSError, match="interrupted delete"):
        s.finalize_current_switch()

    assert s.read_current() == second
    assert b.values["CurrentSwitchCommitted"] == (1, REG_DWORD)
    b.fail_delete_after = None
    if retry == "finalize":
        s.finalize_current_switch()
        s.finalize_current_switch()
    else:
        s.rollback_current_switch()
        s.rollback_current_switch()
    assert s.read_current() == second
    assert not any(name.startswith("PreviousDataRoot") for name in b.values)
    assert "CurrentSwitchCommitted" not in b.values


@pytest.mark.parametrize("stop", range(6))
def test_rollback_clears_interrupted_begin_residue(tmp_path, stop):
    b = Backend()
    s = WindowsDataLocationStore(b)
    first = ref(tmp_path, "first")
    s.begin_current_switch(first)
    s.finalize_current_switch()
    b.writes.clear()
    b.fail_after = stop
    with pytest.raises(OSError, match="interrupted"):
        s.begin_current_switch(ref(tmp_path, "second"))
    b.fail_after = None

    s.rollback_current_switch()
    s.rollback_current_switch()

    assert s.read_current() == first
    assert not any(name.startswith("PreviousDataRoot") for name in b.values)


@pytest.mark.parametrize(
    "name,value",
    [
        ("PreviousDataRootAbsent", True),
        ("CurrentSwitchCommitted", True),
    ],
)
def test_switch_markers_reject_bool_reg_dword(tmp_path, name, value):
    b = Backend()
    s = WindowsDataLocationStore(b)
    current = ref(tmp_path, "current")
    b.values.update(
        {
            DATA_ROOT: (str(current.root), REG_SZ),
            DATA_ROOT_ID: (current.root_id, REG_SZ),
            DATA_ROOT_SCHEMA: (1, REG_DWORD),
            name: (value, REG_DWORD),
        }
    )
    invalid(s.read_current)


def test_pending_recovery_rejects_bool_reg_dword(tmp_path):
    b = Backend()
    b.values[PENDING_MIGRATION_TARGET] = (str(tmp_path / "selected"), REG_SZ)
    b.values["PendingMigrationCreateRecovery"] = (True, REG_DWORD)
    invalid(WindowsDataLocationStore(b).read_pending_migration)


@pytest.mark.parametrize("stop", range(3))
def test_pending_cleanup_write_uses_root_as_commit_field(tmp_path, stop):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    pending = PendingCleanup(ref(tmp_path, "old"))
    backend.fail_after = stop
    if stop < 2:
        with pytest.raises(OSError):
            store.write_pending_cleanup(pending)
        assert store.read_pending_cleanup() is None
    else:
        store.write_pending_cleanup(pending)
        assert store.read_pending_cleanup() == pending
    backend.fail_after = None
    store.clear_pending_cleanup()
    store.write_pending_cleanup(pending)
    assert store.read_pending_cleanup() == pending


@pytest.mark.parametrize("stop", range(3))
def test_pending_cleanup_clear_is_readable_at_every_crash_boundary(tmp_path, stop):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    pending = PendingCleanup(ref(tmp_path, "old"))
    store.write_pending_cleanup(pending)
    backend.fail_delete_after = stop
    if stop < 2:
        with pytest.raises(OSError):
            store.clear_pending_cleanup()
    else:
        store.clear_pending_cleanup()
    assert store.read_pending_cleanup() == (pending if stop == 0 else None)
    backend.fail_delete_after = None
    store.clear_pending_cleanup()
    store.clear_pending_cleanup()
    assert store.read_pending_cleanup() is None
    assert PENDING_CLEANUP_ROOT not in backend.values
    assert PENDING_CLEANUP_ROOT_ID not in backend.values


def test_pending_cleanup_id_residue_is_uncommitted_but_root_requires_id(tmp_path):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    backend.values[PENDING_CLEANUP_ROOT_ID] = ("unused incomplete residue", REG_SZ)
    assert store.read_pending_cleanup() is None
    backend.values.clear()
    backend.values[PENDING_CLEANUP_ROOT] = (str(ref(tmp_path, "old").root), REG_SZ)
    invalid(store.read_pending_cleanup)


@pytest.mark.parametrize(
    "kind", ["relative", "traversal", "nul", "wrong-type", "drive-relative"]
)
def test_pending_cleanup_lexical_path_parser_keeps_validation(tmp_path, kind):
    backend = Backend()
    pending = ref(tmp_path, "old")
    choices = {
        "relative": ("relative/path", REG_SZ),
        "traversal": (str(tmp_path / "old") + "/../EVE Income Tracker", REG_SZ),
        "nul": (str(pending.root) + "\0tail", REG_SZ),
        "wrong-type": (42, REG_DWORD),
        "drive-relative": (tmp_path.drive + "relative", REG_SZ),
    }
    backend.values[PENDING_CLEANUP_ROOT] = choices[kind]
    backend.values[PENDING_CLEANUP_ROOT_ID] = (pending.root_id, REG_SZ)
    invalid(WindowsDataLocationStore(backend).read_pending_cleanup)


def test_pending_cleanup_parser_normalizes_dot_and_separators_lexically(tmp_path):
    backend = Backend()
    pending = ref(tmp_path, "old")
    backend.values[PENDING_CLEANUP_ROOT] = (
        str(tmp_path) + "/old/./EVE Income Tracker",
        REG_SZ,
    )
    backend.values[PENDING_CLEANUP_ROOT_ID] = (pending.root_id, REG_SZ)
    assert WindowsDataLocationStore(backend).read_pending_cleanup() == PendingCleanup(
        pending
    )


@pytest.mark.parametrize("stop", range(3))
def test_pending_migration_write_commits_target_last(tmp_path, stop):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    pending = PendingMigration(tmp_path / "target", False)
    backend.fail_after = stop
    if stop < 2:
        with pytest.raises(OSError):
            store.write_pending_migration(pending)
        assert store.read_pending_migration() is None
    else:
        store.write_pending_migration(pending)
        assert store.read_pending_migration() == pending
    backend.fail_after = None
    store.clear_pending_migration()
    store.write_pending_migration(pending)
    assert store.read_pending_migration() == pending


@pytest.mark.parametrize("stop", range(3))
def test_pending_migration_clear_readable_at_every_boundary(tmp_path, stop):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    pending = PendingMigration(tmp_path / "target", True)
    store.write_pending_migration(pending)
    backend.fail_delete_after = stop
    if stop < 2:
        with pytest.raises(OSError):
            store.clear_pending_migration()
    else:
        store.clear_pending_migration()
    assert store.read_pending_migration() == (pending if stop == 0 else None)
    backend.fail_delete_after = None
    store.clear_pending_migration()
    store.clear_pending_migration()
    assert store.read_pending_migration() is None
    assert PENDING_MIGRATION_TARGET not in backend.values
    assert "PendingMigrationCreateRecovery" not in backend.values


def test_pending_migration_recovery_residue_is_uncommitted_but_target_requires_recovery(
    tmp_path,
):
    backend = Backend()
    store = WindowsDataLocationStore(backend)
    backend.values["PendingMigrationCreateRecovery"] = ("incomplete residue", REG_SZ)
    assert store.read_pending_migration() is None
    backend.values.clear()
    backend.values[PENDING_MIGRATION_TARGET] = (str(tmp_path / "target"), REG_SZ)
    invalid(store.read_pending_migration)
