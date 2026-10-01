"""Current-user data location registry protocol."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
PENDING_MIGRATION_TARGET = "PendingMigrationTarget"
PENDING_MIGRATION_CREATE_RECOVERY = "PendingMigrationCreateRecovery"
PENDING_CLEANUP_ROOT = "PendingCleanupRoot"
PENDING_CLEANUP_ROOT_ID = "PendingCleanupRootId"


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


def _path(value: object, kind: int) -> Path:
    if kind != REG_SZ or not isinstance(value, str) or not value or "\0" in value:
        raise DataLocationStoreError("registry_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DataLocationStoreError("registry_invalid")
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


def _reference(reference: DataRootReference) -> DataRootReference:
    if not isinstance(reference, DataRootReference):
        raise DataLocationStoreError("registry_invalid")
    return DataRootReference(
        _path(str(reference.root), REG_SZ),
        _id(reference.root_id, REG_SZ),
        _schema(reference.schema, REG_DWORD),
    )


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
            if absent != (1, REG_DWORD) or any(
                self.backend.read(name) is not None for name in keys
            ):
                raise DataLocationStoreError("registry_invalid")
            return True, None
        values = [self.backend.read(name) for name in keys]
        if any(value is None for value in values):
            return False, None
        return True, DataRootReference(
            _path(*values[0]), _id(*values[1]), _schema(*values[2])
        )

    def read_current(self) -> DataRootReference | None:
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
        switched, _ = self._previous()
        if (
            not switched
            or self._read_reference(DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA) is None
        ):
            raise DataLocationStoreError("registry_invalid")
        self._clear_previous()

    def rollback_current_switch(self) -> None:
        switched, previous = self._previous()
        if not switched:
            return
        if previous is None:
            for name in (DATA_ROOT, DATA_ROOT_ID, DATA_ROOT_SCHEMA):
                self.backend.delete(name)
        else:
            self._write_current(previous)
        self._clear_previous()

    def read_pending_migration(self) -> PendingMigration | None:
        target = self.backend.read(PENDING_MIGRATION_TARGET)
        recovery = self.backend.read(PENDING_MIGRATION_CREATE_RECOVERY)
        if target is recovery is None:
            return None
        if target is None or recovery not in ((0, REG_DWORD), (1, REG_DWORD)):
            raise DataLocationStoreError("registry_invalid")
        return PendingMigration(_path(*target), bool(recovery[0]))

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
        root_id = self.backend.read(PENDING_CLEANUP_ROOT_ID)
        if root is root_id is None:
            return None
        if root is None or root_id is None:
            raise DataLocationStoreError("registry_invalid")
        return PendingCleanup(DataRootReference(_path(*root), _id(*root_id), 1))

    def write_pending_cleanup(self, pending: PendingCleanup) -> None:
        if not isinstance(pending, PendingCleanup):
            raise DataLocationStoreError("registry_invalid")
        reference = _reference(pending.reference)
        self.backend.write(PENDING_CLEANUP_ROOT_ID, reference.root_id, REG_SZ)
        self.backend.write(PENDING_CLEANUP_ROOT, str(reference.root), REG_SZ)

    def clear_pending_cleanup(self) -> None:
        self.backend.delete(PENDING_CLEANUP_ROOT)
        self.backend.delete(PENDING_CLEANUP_ROOT_ID)


class MemoryDataLocationStore(WindowsDataLocationStore):
    def __init__(self) -> None:
        super().__init__(_MemoryRegistryBackend())
