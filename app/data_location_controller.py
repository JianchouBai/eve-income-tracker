"""Startup and settings orchestration; never creates a database or key.

The desktop owns the single-instance lock and supplies a read-only validator
for supported database schemas and the current user's existing protected key.
"""

from __future__ import annotations

import sqlite3
import tempfile
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
    MigrationTransaction,
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
    _directory_identity,
    _reject_reparse_components,
    _reject_reparse_tree,
    _reject_unsafe_root,
    _validate_reference,
    data_root_from_parent,
    inspect_owned_data_root_removal_path,
    prepare_owned_data_root_removal,
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


_SAFE_MIGRATION_FAILURE_CODES = frozenset(
    {
        "cleanup_failed",
        "cleanup_pending",
        "cleanup_state_failed",
        "copy_failed",
        "database_invalid",
        "destination_invalid",
        "destination_reopen_failed",
        "existing_data_invalid",
        "file_verification_failed",
        "location_missing",
        "location_operation_failed",
        "marker_failed",
        "marker_incomplete",
        "marker_invalid",
        "marker_mismatch",
        "migration_cancel_not_allowed",
        "migration_pending",
        "migration_recovery_pending",
        "path_invalid",
        "path_not_writable",
        "path_reparse",
        "path_unavailable",
        "path_unsafe",
        "recovery_failed",
        "registry_failed",
        "registry_finalize_failed",
        "registry_invalid",
        "registry_rollback_failed",
        "registry_state_unknown",
        "rename_failed",
        "source_invalid",
        "source_not_current",
        "space_insufficient",
        "staging_changed",
        "staging_failed",
        "target_not_empty",
    }
)


@dataclass(frozen=True)
class MigrationFailureInfo:
    code: str
    source: Path
    target: Path
    leftover_paths: tuple[Path, ...]
    can_cancel: bool


@dataclass(frozen=True)
class DataLocationStatus:
    current_path: Path
    volume: str
    usage: dict[str, int]
    recovery: RecoverySnapshotInfo | None
    pending_cleanup: PendingCleanup | None = None
    pending_migration: PendingMigration | None = None
    migration_recovery_pending: bool = False

    @property
    def pending_cleanup_path(self) -> Path | None:
        if self.pending_cleanup is None:
            return None
        if self.pending_cleanup.removal is not None:
            return inspect_owned_data_root_removal_path(self.pending_cleanup.removal)
        return self.pending_cleanup.reference.root


@dataclass(frozen=True)
class _MigrationState:
    current: DataRootReference
    pending: PendingMigration | None
    transaction: MigrationTransaction | None
    cleanup: PendingCleanup | None
    source: Path
    target: Path
    leftover_paths: tuple[Path, ...]


class StartupInteraction(Protocol):
    def choose_initial_location(self, default_root: Path) -> str: ...
    def choose_legacy_action(self, root: Path) -> str: ...
    def choose_data_parent(self) -> Path | None: ...
    def confirm_location(
        self, root: Path, estimate: MigrationEstimate | None = None
    ) -> bool: ...
    def ask_retry_locate_exit(self, code: str) -> str: ...
    def ask_migration_failure(self, info: MigrationFailureInfo) -> str: ...
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
        current_ready_validator: Callable[[AppPaths], None] | None = None,
        recovery_service: RecoverySnapshotService | None = None,
    ) -> None:
        self._store = store
        self._resource_root = resource_root
        self._default_parent = default_parent
        self._existing_validator = existing_validator
        self._current_ready_validator = current_ready_validator or existing_validator
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

    def _validate_contents(
        self,
        paths: AppPaths,
        *,
        validator: Callable[[AppPaths], None] | None = None,
    ) -> None:
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
            (validator or self._existing_validator)(paths)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise DataLocationError("existing_data_invalid") from None

    def _validate(
        self,
        reference: DataRootReference,
        *,
        validator: Callable[[AppPaths], None] | None = None,
    ) -> AppPaths:
        paths = validate_existing_data_root(
            reference, resource_root=self._resource_root
        )
        self._validate_contents(paths, validator=validator)
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

    def _new_marker(self, root: Path, *, state: str = "ready") -> DataRootMarker:
        reference = DataRootReference(root, uuid4().hex, 1)
        stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return DataRootMarker(reference, stamp, state)

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
        marker = self._new_marker(root, state="incomplete")
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
        self._migration.reconcile_interrupted_migration()
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
                marker = read_data_root_marker(current.root)
                if marker.state == "incomplete":
                    self._initializing(current)
                else:
                    self._validate(current, validator=self._current_ready_validator)
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

    def _initializing(
        self, reference: DataRootReference
    ) -> tuple[AppPaths, DataRootMarker]:
        """Authorize only the exact registered fresh-root continuation."""
        if self._store.read_current() != reference:
            raise DataLocationError("marker_mismatch")
        root = _validate_reference(reference)
        _reject_unsafe_root(root, self._resource_root.resolve())
        marker = read_data_root_marker(root)
        if marker.reference != reference or marker.state != "incomplete":
            raise DataLocationError("marker_mismatch")
        _reject_reparse_tree(root)
        with tempfile.TemporaryFile(dir=root):
            pass
        return self._paths(root), marker

    @_safe_operation
    def complete_initialization(self, reference: DataRootReference) -> None:
        """Commit ready only after normal key/database initialization validates."""
        if self._store.read_current() != reference:
            raise DataLocationError("marker_mismatch")
        marker = read_data_root_marker(reference.root)
        if marker.state == "ready":
            self._validate(reference)
            return
        paths, marker = self._initializing(reference)
        identity = _directory_identity(reference.root)
        self._validate_contents(paths)
        # Validation must never authorize a changed pointer, directory or marker.
        _, current_marker = self._initializing(reference)
        if current_marker != marker or _directory_identity(reference.root) != identity:
            raise DataLocationError("marker_mismatch")
        write_data_root_marker(
            reference.root, DataRootMarker(reference, marker.created_at, "ready")
        )

    def _read_migration_state(self) -> _MigrationState:
        current = self._current()
        pending = self._store.read_pending_migration()
        transaction = self._store.read_migration_transaction()
        cleanup = self._store.read_pending_cleanup()
        request_target = (
            data_root_from_parent(pending.target_parent)
            if pending is not None
            else None
        )
        source = current.root
        target = request_target or current.root
        if transaction is not None:
            source = transaction.source.root
            target = transaction.target.root
        elif cleanup is not None and cleanup.reference != current:
            source = cleanup.reference.root
            target = current.root

        leftovers: list[Path] = []
        if transaction is not None:
            try:
                staging, formal, failed = self._migration._transaction_paths(
                    transaction
                )
                candidates = [(staging, "staging")]
                if current != transaction.target:
                    candidates.append((formal, "formal"))
                candidates.append((failed, "diagnostic"))
                for path, kind in candidates:
                    try:
                        if self._migration._validate_transaction_path(
                            path, transaction, kind
                        ):
                            leftovers.append(path)
                    except Exception:  # noqa: BLE001, S112 - fail closed.
                        continue
            except Exception:  # noqa: BLE001, S110 - expose no invalid path.
                pass
        if cleanup is not None:
            if cleanup.removal is not None:
                cleanup_path = inspect_owned_data_root_removal_path(cleanup.removal)
            else:
                try:
                    receipt = prepare_owned_data_root_removal(cleanup.reference)
                except (
                    DataRootValidationError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ):
                    cleanup_path = None
                else:
                    cleanup_path = inspect_owned_data_root_removal_path(receipt)
            if cleanup_path is not None and cleanup_path != current.root:
                leftovers.append(cleanup_path)
        return _MigrationState(
            current,
            pending,
            transaction,
            cleanup,
            source,
            target,
            tuple(dict.fromkeys(leftovers)),
        )

    def _state_can_cancel(self, state: _MigrationState) -> bool:
        if state.pending is None or state.source != state.current.root:
            return False
        try:
            request_target = data_root_from_parent(state.pending.target_parent)
            if state.current.root == request_target:
                return False
            if state.transaction is not None and (
                state.transaction.source != state.current
                or state.transaction.target.root != request_target
            ):
                return False
            self._validate(state.current)
            return (
                self._store.read_current() == state.current
                and self._store.read_pending_migration() == state.pending
                and self._store.read_migration_transaction() == state.transaction
            )
        except Exception:  # noqa: BLE001 - a cancellation predicate fails closed.
            return False

    @_safe_operation
    def migration_failure_info(self, code: str) -> MigrationFailureInfo:
        state = self._read_migration_state()
        safe_code = (
            code
            if isinstance(code, str) and code in _SAFE_MIGRATION_FAILURE_CODES
            else "location_operation_failed"
        )
        return MigrationFailureInfo(
            safe_code,
            state.source,
            state.target,
            state.leftover_paths,
            self._state_can_cancel(state),
        )

    @_safe_operation
    def cancel_pending_migration(self) -> DataRootReference:
        state = self._read_migration_state()
        if not self._state_can_cancel(state):
            raise DataLocationError("migration_cancel_not_allowed")
        try:
            self._store.clear_pending_migration()
        except (OSError, ValueError, RuntimeError):
            try:
                if self._store.read_pending_migration() is None:
                    return state.current
            except (OSError, ValueError, RuntimeError):
                pass
            raise DataLocationError("registry_failed") from None
        if self._store.read_pending_migration() is not None:
            raise DataLocationError("registry_failed")
        return state.current

    @_safe_operation
    def execute_pending_migration(self) -> MigrationResult | None:
        self._migration.reconcile_interrupted_migration()
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
        if self._store.read_migration_transaction() is not None:
            raise DataLocationError("migration_recovery_pending")
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
            current_path=current.root,
            volume=current.root.anchor,
            usage=usage,
            recovery=recovery,
            pending_cleanup=self._store.read_pending_cleanup(),
            pending_migration=self._store.read_pending_migration(),
            migration_recovery_pending=(
                self._store.read_migration_transaction() is not None
            ),
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
