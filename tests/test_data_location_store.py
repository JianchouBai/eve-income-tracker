from uuid import uuid4

import pytest

from app.data_location_store import (
    DATA_ROOT,
    DATA_ROOT_ID,
    DATA_ROOT_SCHEMA,
    PENDING_CLEANUP_ROOT,
    PENDING_MIGRATION_TARGET,
    PREVIOUS_DATA_ROOT,
    PREVIOUS_DATA_ROOT_ID,
    PREVIOUS_DATA_ROOT_SCHEMA,
    REG_DWORD,
    REG_SZ,
    DataLocationStoreError,
    MemoryDataLocationStore,
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

    def read(self, name):
        return self.values.get(name)

    def write(self, name, value, kind):
        if self.fail_after is not None and len(self.writes) == self.fail_after:
            raise OSError("interrupted")
        self.values[name] = (value, kind)
        self.writes.append(name)

    def delete(self, name):
        self.values.pop(name, None)


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
