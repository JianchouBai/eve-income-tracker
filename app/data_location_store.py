"""Current-user data location registry protocol."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.data_roots import DataRootReference

REGISTRY_KEY = r"Software\JianchouBai\EVE Income Tracker"
REG_SZ = 1
REG_DWORD = 4
DATA_ROOT = "DataRoot"
DATA_ROOT_ID = "DataRootId"
DATA_ROOT_SCHEMA = "DataRootSchema"
PREVIOUS_DATA_ROOT = "PreviousDataRoot"
PREVIOUS_DATA_ROOT_ID = "PreviousDataRootId"
PREVIOUS_DATA_ROOT_SCHEMA = "PreviousDataRootSchema"
PREVIOUS_DATA_ROOT_ABSENT = "PreviousDataRootAbsent"
CURRENT_SWITCH_COMMITTED = "CurrentSwitchCommitted"
PENDING_MIGRATION_TARGET = "PendingMigrationTarget"
PENDING_MIGRATION_CREATE_RECOVERY = "PendingMigrationCreateRecovery"
PENDING_CLEANUP_ROOT = "PendingCleanupRoot"
PENDING_CLEANUP_ROOT_ID = "PendingCleanupRootId"


MIGRATION_TRANSACTION = "MigrationTransaction"
_MIGRATION_TRANSACTION_FIELDS = frozenset(
    {
        "version",
        "operation_id",
        "source",
        "target",
        "created_at",
        "staging_identity",
    }
)
_MIGRATION_REFERENCE_FIELDS = frozenset({"root", "root_id", "schema"})


class DataLocationStoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PendingMigration:
    target_parent: Path
    create_recovery: bool


@dataclass(frozen=True)
class PendingCleanup:
    reference: DataRootReference


@dataclass(frozen=True)
class MigrationTransaction:
    version: int
    operation_id: str
    source: DataRootReference
    target: DataRootReference
    created_at: str
    staging_identity: tuple[int, int] | None


class RegistryBackend(Protocol):
    def read(self, name: str) -> tuple[object, int] | None: ...
    def write(self, name: str, value: object, kind: int) -> None: ...
    def delete(self, name: str) -> None: ...


class DataLocationStore(Protocol):
    def read_current(self) -> DataRootReference | None: ...
    def begin_current_switch(self, reference: DataRootReference) -> None: ...
    def finalize_current_switch(self) -> None: ...
    def rollback_current_switch(self) -> None: ...
    def read_pending_migration(self) -> PendingMigration | None: ...
    def write_pending_migration(self, pending: PendingMigration) -> None: ...
    def clear_pending_migration(self) -> None: ...
    def read_pending_cleanup(self) -> PendingCleanup | None: ...
    def write_pending_cleanup(self, pending: PendingCleanup) -> None: ...
    def clear_pending_cleanup(self) -> None: ...
    def read_migration_transaction(self) -> MigrationTransaction | None: ...
    def write_migration_transaction(self, value: MigrationTransaction) -> None: ...
    def clear_migration_transaction(self) -> None: ...


def _path(value: object, kind: int, *, resolve_links: bool = True) -> Path:
    if kind != REG_SZ or not isinstance(value, str) or not value or "\0" in value:
        raise DataLocationStoreError("registry_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DataLocationStoreError("registry_invalid")
    if not resolve_links:
        return path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise DataLocationStoreError("registry_invalid") from None


def _id(value: object, kind: int) -> str:
    if kind != REG_SZ or not isinstance(value, str) or "\0" in value:
        raise DataLocationStoreError("registry_invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise DataLocationStoreError("registry_invalid") from None
    if parsed.version != 4:
        raise DataLocationStoreError("registry_invalid")
    return value


def _schema(value: object, kind: int) -> int:
    if kind != REG_DWORD or type(value) is not int or value != 1:
        raise DataLocationStoreError("registry_invalid")
    return value


def _dword(value: tuple[object, int] | None, allowed: tuple[int, ...]) -> int:
    if (
        value is None
        or value[1] != REG_DWORD
        or type(value[0]) is not int
        or value[0] not in allowed
    ):
        raise DataLocationStoreError("registry_invalid")
    return value[0]


def _reference(reference: DataRootReference) -> DataRootReference:
    if not isinstance(reference, DataRootReference):
        raise DataLocationStoreError("registry_invalid")
    return DataRootReference(
        _path(str(reference.root), REG_SZ),
        _id(reference.root_id, REG_SZ),
        _schema(reference.schema, REG_DWORD),
    )


def _transaction_id(value: object) -> str:
    if not isinstance(value, str):
        raise DataLocationStoreError("registry_invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise DataLocationStoreError("registry_invalid") from None
    if parsed.version != 4 or value != parsed.hex:
        raise DataLocationStoreError("registry_invalid")
    return value


def _transaction_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DataLocationStoreError("registry_invalid")
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        raise DataLocationStoreError("registry_invalid") from None
    if created.utcoffset() is None or created.utcoffset().total_seconds() != 0:
        raise DataLocationStoreError("registry_invalid")
    return value


def _transaction_identity(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(part) is not int for part in value)
        or value[0] < 0
        or value[1] <= 0
    ):
        raise DataLocationStoreError("registry_invalid")
    return value[0], value[1]


def _transaction_reference(value: object) -> DataRootReference:
    if not isinstance(value, dict) or set(value) != _MIGRATION_REFERENCE_FIELDS:
        raise DataLocationStoreError("registry_invalid")
    return DataRootReference(
        _path(value["root"], REG_SZ, resolve_links=False),
        _id(value["root_id"], REG_SZ),
        _schema(value["schema"], REG_DWORD),
    )


def _transaction(value: object) -> MigrationTransaction:
    if not isinstance(value, MigrationTransaction):
        raise DataLocationStoreError("registry_invalid")
    if type(value.version) is not int or value.version != 1:
        raise DataLocationStoreError("registry_invalid")
    if not isinstance(value.source, DataRootReference) or not isinstance(
        value.target, DataRootReference
    ):
        raise DataLocationStoreError("registry_invalid")
    source = _transaction_reference(
        {
            "root": str(value.source.root),
            "root_id": value.source.root_id,
            "schema": value.source.schema,
        }
    )
    target = _transaction_reference(
        {
            "root": str(value.target.root),
            "root_id": value.target.root_id,
            "schema": value.target.schema,
        }
    )
    if source.root == target.root:
        raise DataLocationStoreError("registry_invalid")
    return MigrationTransaction(
        value.version,
        _transaction_id(value.operation_id),
        source,
        target,
        _transaction_timestamp(value.created_at),
        _transaction_identity(value.staging_identity),
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise DataLocationStoreError("registry_invalid")
        value[name] = item
    return value


class _WindowsRegistryBackend:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows registry is only available on Windows")
        import winreg

        self.winreg = winreg

    def read(self, name: str) -> tuple[object, int] | None:
        w = self.winreg
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
                return w.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def write(self, name: str, value: object, kind: int) -> None:
        w = self.winreg
        with w.CreateKeyEx(w.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            w.SetValueEx(key, name, 0, kind, value)

    def delete(self, name: str) -> None:
        w = self.winreg
        try:
            with w.OpenKey(
                w.HKEY_CURRENT_USER, REGISTRY_KEY, 0, w.KEY_SET_VALUE
            ) as key:
                w.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class _MemoryRegistryBackend:
    def __init__(self) -> None:
        self.values: dict[str, tuple[object, int]] = {}

    def read(self, name: str) -> tuple[object, int] | None:
        return self.values.get(name)

    def write(self, name: str, value: object, kind: int) -> None:
        self.values[name] = (value, kind)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class WindowsDataLocationStore:
    def __init__(self, backend: RegistryBackend | None = None) -> None:
        self.backend = backend if backend is not None else _WindowsRegistryBackend()

    def _read_reference(
        self, root: str, root_id: str, schema: str
    ) -> DataRootReference | None:
        values = [self.backend.read(name) for name in (root, root_id, schema)]
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise DataLocationStoreError("registry_invalid")
        return DataRootReference(
            _path(*values[0]), _id(*values[1]), _schema(*values[2])
        )

    def _previous(self) -> tuple[bool, DataRootReference | None]:
        absent = self.backend.read(PREVIOUS_DATA_ROOT_ABSENT)
        keys = (PREVIOUS_DATA_ROOT, PREVIOUS_DATA_ROOT_ID, PREVIOUS_DATA_ROOT_SCHEMA)
        if absent is not None:
            _dword(absent, (1,))
            if any(self.backend.read(name) is not None for name in keys):
                raise DataLocationStoreError("registry_invalid")
            return True, None
        values = [self.backend.read(name) for name in keys]
        if any(value is None for value in values):
            return False, None
        return True, DataRootReference(
            _path(*values[0]), _id(*values[1]), _schema(*values[2])
        )

    def _committed(self) -> bool:
        marker = self.backend.read(CURRENT_SWITCH_COMMITTED)
        if marker is None:
            return False
        _dword(marker, (1,))
        return True

    def _has_previous_residue(self) -> bool:
        return any(
            self.backend.read(name) is not None
            for name in (
                PREVIOUS_DATA_ROOT,
                PREVIOUS_DATA_ROOT_ID,
                PREVIOUS_DATA_ROOT_SCHEMA,
                PREVIOUS_DATA_ROOT_ABSENT,
            )
        )

    def read_current(self) -> DataRootReference | None:
        if self._committed():
            current = self._read_reference(DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
            if current is None:
                raise DataLocationStoreError("registry_invalid")
            return current
        switched, previous = self._previous()
        return (
            previous
            if switched
            else self._read_reference(DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
        )

    def _write_current(self, reference: DataRootReference) -> None:
        self.backend.write(DATA_ROOT_SCHEMA, reference.schema, REG_DWORD)
        self.backend.write(DATA_ROOT_ID, reference.root_id, REG_SZ)
        self.backend.write(DATA_ROOT, str(reference.root), REG_SZ)

    def _clear_previous(self) -> None:
        for name in (
            PREVIOUS_DATA_ROOT,
            PREVIOUS_DATA_ROOT_ID,
            PREVIOUS_DATA_ROOT_SCHEMA,
            PREVIOUS_DATA_ROOT_ABSENT,
        ):
            self.backend.delete(name)

    def begin_current_switch(self, reference: DataRootReference) -> None:
        target = _reference(reference)
        if self._committed():
            self.finalize_current_switch()
        switched, _ = self._previous()
        if switched:
            raise DataLocationStoreError("registry_invalid")
        current = self._read_reference(DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
        self._clear_previous()
        if current is None:
            self.backend.write(PREVIOUS_DATA_ROOT_ABSENT, 1, REG_DWORD)
        else:
            self.backend.write(PREVIOUS_DATA_ROOT_ID, current.root_id, REG_SZ)
            self.backend.write(PREVIOUS_DATA_ROOT_SCHEMA, current.schema, REG_DWORD)
            self.backend.write(PREVIOUS_DATA_ROOT, str(current.root), REG_SZ)
        self._write_current(target)

    def finalize_current_switch(self) -> None:
        committed = self._committed()
        if not committed:
            switched, _ = self._previous()
            if not switched and self._has_previous_residue():
                raise DataLocationStoreError("registry_invalid")
        current = self._read_reference(DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
        if current is None:
            raise DataLocationStoreError("registry_invalid")
        if not committed and switched:
            self.backend.write(CURRENT_SWITCH_COMMITTED, 1, REG_DWORD)
        self._clear_previous()
        self.backend.delete(CURRENT_SWITCH_COMMITTED)

    def rollback_current_switch(self) -> None:
        if self._committed():
            self.finalize_current_switch()
            return
        switched, previous = self._previous()
        if not switched:
            if self._has_previous_residue():
                if (
                    self._read_reference(DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA)
                    is None
                ):
                    raise DataLocationStoreError("registry_invalid")
                self._clear_previous()
            return
        if previous is None:
            for name in (DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA):
                self.backend.delete(name)
        else:
            self._write_current(previous)
        self._clear_previous()

    def read_pending_migration(self) -> PendingMigration | None:
        target = self.backend.read(PENDING_MIGRATION_TARGET)
        # Target is the commit field: recovery-only residue from an interrupted
        # write/clear never authorizes a migration.
        if target is None:
            return None
        recovery = self.backend.read(PENDING_MIGRATION_CREATE_RECOVERY)
        create_recovery = _dword(recovery, (0, 1))
        return PendingMigration(_path(*target), bool(create_recovery))

    def write_pending_migration(self, pending: PendingMigration) -> None:
        if (
            not isinstance(pending, PendingMigration)
            or type(pending.create_recovery) is not bool
        ):
            raise DataLocationStoreError("registry_invalid")
        parent = _path(str(pending.target_parent), REG_SZ)
        self.backend.write(
            PENDING_MIGRATION_CREATE_RECOVERY, int(pending.create_recovery), REG_DWORD
        )
        self.backend.write(PENDING_MIGRATION_TARGET, str(parent), REG_SZ)

    def clear_pending_migration(self) -> None:
        self.backend.delete(PENDING_MIGRATION_TARGET)
        self.backend.delete(PENDING_MIGRATION_CREATE_RECOVERY)

    def read_pending_cleanup(self) -> PendingCleanup | None:
        root = self.backend.read(PENDING_CLEANUP_ROOT)
        # Root is the commit field: ID-only residue is an interrupted write
        # or clear, never an actionable cleanup request.
        if root is None:
            return None
        root_id = self.backend.read(PENDING_CLEANUP_ROOT_ID)
        if root_id is None:
            raise DataLocationStoreError("registry_invalid")
        # Preserve the registered path so cleanup can reject a parent that
        # became a junction instead of silently resolving to its new target.
        return PendingCleanup(
            DataRootReference(_path(*root, resolve_links=False), _id(*root_id), 1)
        )

    def write_pending_cleanup(self, pending: PendingCleanup) -> None:
        if not isinstance(pending, PendingCleanup):
            raise DataLocationStoreError("registry_invalid")
        reference = _reference(pending.reference)
        self.backend.write(PENDING_CLEANUP_ROOT_ID, reference.root_id, REG_SZ)
        self.backend.write(PENDING_CLEANUP_ROOT, str(reference.root), REG_SZ)

    def clear_pending_cleanup(self) -> None:
        self.backend.delete(PENDING_CLEANUP_ROOT)
        self.backend.delete(PENDING_CLEANUP_ROOT_ID)

    def read_migration_transaction(self) -> MigrationTransaction | None:
        encoded = self.backend.read(MIGRATION_TRANSACTION)
        if encoded is None:
            return None
        if encoded[1] != REG_SZ or not isinstance(encoded[0], str):
            raise DataLocationStoreError("registry_invalid")
        try:
            payload = json.loads(encoded[0], object_pairs_hook=_strict_json_object)
        except (json.JSONDecodeError, DataLocationStoreError):
            raise DataLocationStoreError("registry_invalid") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != _MIGRATION_TRANSACTION_FIELDS
        ):
            raise DataLocationStoreError("registry_invalid")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise DataLocationStoreError("registry_invalid")
        value = MigrationTransaction(
            payload["version"],
            _transaction_id(payload["operation_id"]),
            _transaction_reference(payload["source"]),
            _transaction_reference(payload["target"]),
            _transaction_timestamp(payload["created_at"]),
            _transaction_identity(payload["staging_identity"]),
        )
        return _transaction(value)

    def write_migration_transaction(self, value: MigrationTransaction) -> None:
        record = _transaction(value)
        payload = {
            "version": record.version,
            "operation_id": record.operation_id,
            "source": {
                "root": str(record.source.root),
                "root_id": record.source.root_id,
                "schema": record.source.schema,
            },
            "target": {
                "root": str(record.target.root),
                "root_id": record.target.root_id,
                "schema": record.target.schema,
            },
            "created_at": record.created_at,
            "staging_identity": record.staging_identity,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.backend.write(MIGRATION_TRANSACTION, encoded, REG_SZ)

    def clear_migration_transaction(self) -> None:
        self.backend.delete(MIGRATION_TRANSACTION)


class MemoryDataLocationStore(WindowsDataLocationStore):
    def __init__(self) -> None:
        super().__init__(_MemoryRegistryBackend())
