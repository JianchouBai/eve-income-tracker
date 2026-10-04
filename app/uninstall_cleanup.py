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
    RegistryBackend,
    WindowsDataLocationStore,
)
from app.data_roots import (
    DataRootReference,
    DataRootValidationError,
    remove_owned_data_root,
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
    PENDING_CLEANUP_ROOT,
    PENDING_CLEANUP_ROOT_ID,
    PENDING_CLEANUP_QUARANTINE,
)
_ALL = _CURRENT + _UNSTABLE


def cleanup_uninstall_data(
    expected_root: str,
    expected_id: str,
    *,
    registry_backend: RegistryBackend | None = None,
) -> int:
    """Return zero only after exact current state and owned tree removal.

    The caller owns registry deletion. Every failure leaves it untouched.
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
        backend = WindowsDataLocationStore(registry_backend).backend
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
        remove_owned_data_root(DataRootReference(root, expected_id, 1))
        return 0 if not os.path.lexists(root) else 2
    except (DataRootValidationError, OSError, RuntimeError, TypeError, ValueError):
        return 2
