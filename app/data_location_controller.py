"""Startup and settings orchestration; never creates a database or key.

The desktop owns the single-instance lock and supplies a read-only validator
for supported database schemas and the current user's existing protected key.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.data_location_store import (
    DataLocationStore,
    DataLocationStoreError,
    PendingCleanup,
    PendingMigration,
)
from app.data_migration import (
    DataMigrationError,
    DataMigrationService,
    MigrationEstimate,
    MigrationResult,
)
from app.data_roots import (
    MARKER_NAME,
    DataRootMarker,
    DataRootReference,
    DataRootValidationError,
    _reject_reparse_components,
    _reject_reparse_tree,
    _reject_unsafe_root,
    data_root_from_parent,
    read_data_root_marker,
    relocate_existing_data_root,
    validate_existing_data_root,
    validate_new_data_root,
    write_data_root_marker,
)
from app.migration_recovery import (
    RecoverySnapshotError,
    RecoverySnapshotInfo,
    RecoverySnapshotService,
)
from app.paths import APP_DATA_DIRECTORY, AppPaths, resolve_app_paths
from app.windows_runtime import WindowsRuntimeError


class DataLocationError(ValueError):
    """Stable, private-data-free controller failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DataLocationStatus:
    current_path: Path
    volume: str
    usage: dict[str, int]
    recovery: RecoverySnapshotInfo | None
    pending_cleanup: PendingCleanup | None = None
    pending_migration: PendingMigration | None = None


class StartupInteraction(Protocol):
    def choose_initial_location(self, default_root: Path) -> str: ...
    def choose_legacy_action(self, root: Path) -> str: ...
    def choose_data_parent(self) -> Path | None: ...
    def confirm_location(
        self, root: Path, estimate: MigrationEstimate | None = None
    ) -> bool: ...
    def ask_retry_locate_exit(self, code: str) -> str: ...
    def choose_existing_data_root(self) -> Path | None: ...


def _safe_operation(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DataLocationError:
            raise
        except (
            DataRootValidationError,
            DataLocationStoreError,
            DataMigrationError,
            RecoverySnapshotError,
            WindowsRuntimeError,
        ) as exc:
            raise DataLocationError(exc.code) from None
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise DataLocationError("location_operation_failed") from None

    return wrapped


class DataLocationController:
    def __init__(
        self,
        store: DataLocationStore,
        *,
        resource_root: Path,
        default_parent: Path,
        existing_validator: Callable[[AppPaths], None],
        recovery_service: RecoverySnapshotService | None = None,
    ) -> None:
        self._store = store
        self._resource_root = resource_root
        self._default_parent = default_parent
        self._existing_validator = existing_validator
        self._recovery = recovery_service or RecoverySnapshotService()
        self._migration = DataMigrationService(
            store,
            resource_root=resource_root,
            destination_validator=existing_validator,
            recovery_service=self._recovery,
        )

    def _paths(self, root: Path) -> AppPaths:
        return resolve_app_paths(
            frozen=True, bundle_root=self._resource_root, data_root=root
        )

    def _validate_contents(self, paths: AppPaths) -> None:
        _reject_reparse_components(paths.data_root)
        _reject_reparse_tree(paths.data_root)
        if (
            not paths.database_path.is_file()
            or not paths.database_path.stat().st_size
            or not paths.key_path.is_file()
            or not paths.key_path.stat().st_size
        ):
            raise DataLocationError("existing_data_invalid")
        try:
            self._existing_validator(paths)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise DataLocationError("existing_data_invalid") from None

    def _validate(self, reference: DataRootReference) -> AppPaths:
        paths = validate_existing_data_root(
            reference, resource_root=self._resource_root
        )
        self._validate_contents(paths)
        return paths

    def _current(self) -> DataRootReference:
        current = self._store.read_current()
        if current is None:
            raise DataLocationError("location_missing")
        return current

    def _switch(self, reference: DataRootReference) -> None:
        try:
            self._store.begin_current_switch(reference)
            self._store.finalize_current_switch()
        except (OSError, ValueError, RuntimeError):
            try:
                if self._store.read_current() == reference:
                    raise DataLocationError("registry_finalize_failed")
                self._store.rollback_current_switch()
            except DataLocationError:
                raise
            except (OSError, ValueError, RuntimeError):
                raise DataLocationError("registry_rollback_failed") from None
            raise DataLocationError("registry_failed") from None

    @_safe_operation
    def reconnect_existing(self, root: Path) -> DataRootReference:
        current = self._store.read_current()
        pending = self._store.read_pending_cleanup()
        if pending is not None and pending.reference.root == root.resolve():
            raise DataLocationError("cleanup_pending")
        try:
            reference = read_data_root_marker(root).reference
        except DataRootValidationError as exc:
            if exc.code != "marker_mismatch" or current is None:
                raise
            reference = relocate_existing_data_root(
                root,
                current,
                resource_root=self._resource_root,
                existing_validator=self._validate_contents,
            )
        if current is not None and reference.root_id != current.root_id:
            raise DataLocationError("marker_mismatch")
        self._validate(reference)
        self._switch(reference)
        return reference

    def _new_marker(self, root: Path) -> DataRootMarker:
        reference = DataRootReference(root, uuid4().hex, 1)
        stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return DataRootMarker(reference, stamp, "ready")

    def _fresh(self, interaction: StartupInteraction) -> DataRootReference:
        default = self._default_parent / APP_DATA_DIRECTORY
        choice = interaction.choose_initial_location(default)
        if choice == "default":
            parent = self._default_parent
        elif choice == "custom":
            parent = interaction.choose_data_parent()
        else:
            parent = None
        if parent is None:
            raise DataLocationError("startup_cancelled")
        root = validate_new_data_root(
            parent, source_root=self._resource_root, install_root=self._resource_root
        )
        if not interaction.confirm_location(root):
            raise DataLocationError("startup_cancelled")
        # Recheck after UI: another process may have occupied the target.
        validate_new_data_root(
            parent, source_root=self._resource_root, install_root=self._resource_root
        )
        created = not root.exists()
        marker = self._new_marker(root)
        try:
            root.mkdir(exist_ok=True)
            write_data_root_marker(root, marker)
            self._switch(marker.reference)
        except (OSError, ValueError, RuntimeError):
            # A committed switch, even with journal cleanup failure, owns the
            # prepared root. Never remove its marker.
            if self._store.read_current() != marker.reference:
                if (
                    root.exists()
                    and (root / MARKER_NAME).exists()
                    and read_data_root_marker(root) == marker
                ):
                    (root / MARKER_NAME).unlink()
                if created and root.exists():
                    root.rmdir()
            raise
        return marker.reference

    def _is_legacy(self, legacy_root: Path) -> bool:
        expected = self._default_parent / APP_DATA_DIRECTORY
        if legacy_root != expected:
            return False
        _reject_reparse_components(legacy_root)
        if not (legacy_root / "data/eve_income.sqlite3").is_file():
            return False
        _reject_unsafe_root(legacy_root.resolve(), self._resource_root.resolve())
        self._validate_contents(self._paths(legacy_root))
        return True

    def _legacy(self, root: Path, interaction: StartupInteraction) -> DataRootReference:
        choice = interaction.choose_legacy_action(root)
        if choice not in ("keep", "migrate"):
            raise DataLocationError("startup_cancelled")
        parent = interaction.choose_data_parent() if choice == "migrate" else None
        if choice == "migrate" and parent is None:
            raise DataLocationError("startup_cancelled")
        if parent is not None:
            validate_new_data_root(
                parent, source_root=root, install_root=self._resource_root
            )
        self._validate_contents(self._paths(root))
        added_marker = not (root / MARKER_NAME).exists()
        marker = (
            self._new_marker(root.resolve())
            if added_marker
            else read_data_root_marker(root)
        )
        accepted = False
        try:
            if added_marker:
                write_data_root_marker(root, marker)
            self._validate(marker.reference)
            estimate = (
                None
                if parent is None
                else self._migration.estimate(marker.reference, parent)
            )
            target = root if estimate is None else estimate.target_root
            if not interaction.confirm_location(target, estimate):
                raise DataLocationError("startup_cancelled")
            accepted = True
            self._switch(marker.reference)
            if parent is not None:
                self.schedule_migration(parent)
            return marker.reference
        finally:
            if added_marker and not accepted and read_data_root_marker(root) == marker:
                (root / MARKER_NAME).unlink()

    @_safe_operation
    def resolve_startup(
        self,
        legacy_root: Path,
        interaction: StartupInteraction,
    ) -> DataRootReference:
        # Task 2 rolls an interrupted uncommitted switch back, or finishes the
        # committed journal. Do this before trying to start a new switch.
        self._store.rollback_current_switch()
        current = self._store.read_current()
        if current is None:
            if (
                self._store.read_pending_cleanup()
                or self._store.read_pending_migration()
            ):
                raise DataLocationError("registry_invalid")
            if self._is_legacy(legacy_root):
                return self._legacy(legacy_root, interaction)
            return self._fresh(interaction)
        while True:
            try:
                self._validate(current)
                return current
            except (DataLocationError, DataRootValidationError) as exc:
                code = exc.code
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                code = "location_unavailable"
            action = interaction.ask_retry_locate_exit(code)
            if action == "retry":
                current = self._current()
            elif action == "locate":
                root = interaction.choose_existing_data_root()
                if root is None:
                    raise DataLocationError("startup_cancelled")
                try:
                    return self.reconnect_existing(root)
                except DataLocationError:
                    # Re-enter recovery; the current pointer is still authoritative.
                    current = self._current()
            else:
                raise DataLocationError("startup_cancelled")

    @_safe_operation
    def execute_pending_migration(self) -> MigrationResult | None:
        pending = self._store.read_pending_migration()
        if pending is None:
            # Retire recovery-only residue from an interrupted write/clear.
            self._store.clear_pending_migration()
            if self._store.read_pending_cleanup() is None:
                return None
        current = self._current()
        # Never authorize deletion or retire a request from an invalid root.
        self._validate(current)
        if pending is not None:
            _reject_reparse_components(pending.target_parent)
            target = data_root_from_parent(pending.target_parent)
            if current.root == target:
                # A crash can leave a committed selection with either journal
                # still pending. Complete these before touching the old root.
                self._store.finalize_current_switch()
                if self._store.read_current() != current:
                    raise DataLocationError("registry_state_unknown")
                self._store.clear_pending_migration()
                self._migration.retry_pending_cleanup()
                return None
        self._migration.retry_pending_cleanup()
        if pending is None:
            return None
        if self._store.read_pending_cleanup() is not None:
            raise DataLocationError("cleanup_pending")
        return self._migration.migrate(
            current, pending.target_parent, create_recovery=pending.create_recovery
        )

    @_safe_operation
    def estimate(
        self, target_parent: Path, *, create_recovery: bool = True
    ) -> MigrationEstimate:
        if type(create_recovery) is not bool:
            raise DataLocationError("recovery_option_invalid")
        return self._migration.estimate(
            self._current(), target_parent, create_recovery=create_recovery
        )

    @_safe_operation
    def schedule_migration(
        self, target_parent: Path, *, create_recovery: bool = True
    ) -> MigrationEstimate:
        if self._store.read_pending_cleanup() is not None:
            raise DataLocationError("cleanup_pending")
        if self._store.read_pending_migration() is not None:
            raise DataLocationError("migration_pending")
        estimate = self.estimate(target_parent, create_recovery=create_recovery)
        if estimate.free_bytes < estimate.copy_bytes + estimate.recovery_bytes + 65536:
            raise DataLocationError("space_insufficient")
        self._store.write_pending_migration(
            PendingMigration(estimate.target_root.parent, create_recovery)
        )
        return estimate

    @_safe_operation
    def status(self) -> DataLocationStatus:
        current = self._current()
        paths = validate_existing_data_root(
            current, resource_root=self._resource_root, require_writable=False
        )
        _reject_reparse_tree(paths.data_root)
        recovery = self._recovery.inspect(paths)
        usage = dict.fromkeys(
            ("database", "config", "logs", "webview", "backups", "recovery", "other"), 0
        )
        for path in paths.data_root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(paths.data_root)
            if recovery is not None and path == recovery.archive_path:
                category = "recovery"
            elif paths.webview_storage_dir in path.parents:
                category = "webview"
            elif relative.parts[0] == "data":
                category = "database"
            elif relative.parts[0] in ("config", "logs", "backups"):
                category = relative.parts[0]
            else:
                category = "other"
            usage[category] += path.stat().st_size
        usage["total"] = sum(usage.values())
        return DataLocationStatus(
            current.root,
            current.root.anchor,
            usage,
            recovery,
            self._store.read_pending_cleanup(),
            self._store.read_pending_migration(),
        )

    @_safe_operation
    def delete_recovery(self) -> int:
        paths = validate_existing_data_root(
            self._current(), resource_root=self._resource_root
        )
        _reject_reparse_tree(paths.data_root)
        info = self._recovery.inspect(paths)
        return (
            info.size_bytes if info is not None and self._recovery.delete(paths) else 0
        )

    @_safe_operation
    def record_success(self) -> RecoverySnapshotInfo | None:
        paths = self._validate(self._current())
        return self._recovery.record_success_and_prune(paths)
