"""Narrow, silent uninstall data removal through the owned-root handle boundary."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

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
    PendingCleanup,
    RegistryBackend,
    WindowsDataLocationStore,
)
from app.data_roots import (
    DataRootReference,
    DataRootValidationError,
    prepare_owned_data_root_removal,
    resume_owned_data_root_removal,
)

_CURRENT = (DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
_UNSTABLE = (
    PREVIOUS_DATA_ROOT,
    PREVIOUS_DATA_ROOT_ID,
    PREVIOUS_DATA_ROOT_SCHEMA,
    PREVIOUS_DATA_ROOT_ABSENT,
    CURRENT_SWITCH_COMMITTED,
    MIGRATION_TRANSACTION,
    PENDING_MIGRATION_TARGET,
    PENDING_MIGRATION_CREATE_RECOVERY,
)
_CLEANUP = (
    PENDING_CLEANUP_ROOT,
    PENDING_CLEANUP_ROOT_ID,
    PENDING_CLEANUP_QUARANTINE,
)
_ALL = _CURRENT + _UNSTABLE + _CLEANUP


def cleanup_uninstall_data(
    expected_root: str,
    expected_id: str,
    *,
    registry_backend: RegistryBackend | None = None,
) -> int:
    """Return zero only after exact current state and owned tree removal.

    The caller owns final registry deletion. Failures and interruptions retain
    any published receipt; success also retains it until Inno clears the registry.
    A rerun resumes only that exact journal, including after completed deletion.
    """
    try:
        if not isinstance(expected_root, str) or not isinstance(expected_id, str):
            return 2
        root = Path(expected_root)
        root_uuid = UUID(expected_id)
        if (
            not root.is_absolute()
            or ".." in root.parts
            or str(root) != expected_root
            or root_uuid.version != 4
            or expected_id not in (str(root_uuid), root_uuid.hex)
        ):
            return 2
        store = WindowsDataLocationStore(registry_backend)
        backend = store.backend
        snapshot = {name: backend.read(name) for name in _ALL}
        if any(snapshot[name] is not None for name in _UNSTABLE):
            return 2
        if snapshot[DATA_ROOT] != (expected_root, REG_SZ):
            return 2
        if snapshot[DATA_ROOT_ID] != (expected_id, REG_SZ):
            return 2
        if snapshot[DATA_ROOT_SCHEMA] != (1, REG_DWORD):
            return 2
        if any(backend.read(name) != snapshot[name] for name in _ALL):
            return 2
        reference = DataRootReference(root, expected_id, 1)
        if any(snapshot[name] is not None for name in _CLEANUP):
            if any(snapshot[name] is None for name in _CLEANUP):
                return 2
            pending = store.read_pending_cleanup()
            if (
                pending is None
                or pending.reference != reference
                or pending.removal is None
                or snapshot[PENDING_CLEANUP_ROOT] != (expected_root, REG_SZ)
                or snapshot[PENDING_CLEANUP_ROOT_ID] != (expected_id, REG_SZ)
            ):
                return 2
            if any(backend.read(name) != snapshot[name] for name in _ALL):
                return 2
            # A prior flush may have failed with the receipt still cached.
            # Rewriting its exact bytes makes retries cross the same barrier.
            backend.write(
                PENDING_CLEANUP_QUARANTINE, *snapshot[PENDING_CLEANUP_QUARANTINE]
            )
        else:
            pending = PendingCleanup(
                reference, prepare_owned_data_root_removal(reference)
            )
            if any(backend.read(name) != snapshot[name] for name in _ALL):
                return 2
            store.write_pending_cleanup(pending)

        journal = {name: backend.read(name) for name in _ALL}
        if (
            any(journal[name] != snapshot[name] for name in _CURRENT + _UNSTABLE)
            or journal[PENDING_CLEANUP_ROOT] != (expected_root, REG_SZ)
            or journal[PENDING_CLEANUP_ROOT_ID] != (expected_id, REG_SZ)
            or store.read_pending_cleanup() != pending
            or any(backend.read(name) != journal[name] for name in _ALL)
        ):
            return 2
        receipt = pending.removal
        # Migration cleanup may retain a foreign original while deleting its
        # quarantine. Explicit uninstall instead refuses that ambiguous state.
        if os.path.lexists(root):
            details = root.lstat()
            if (details.st_dev, details.st_ino) != receipt.identity:
                return 2
        resume_owned_data_root_removal(receipt)
        if (
            os.path.lexists(root)
            or os.path.lexists(receipt.quarantine)
            or any(backend.read(name) != journal[name] for name in _ALL)
        ):
            return 2
        return 0
    except (DataRootValidationError, OSError, RuntimeError, TypeError, ValueError):
        return 2
