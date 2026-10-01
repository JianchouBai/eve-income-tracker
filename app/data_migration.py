"""Transactional, offline migration of an owned application data root.

The desktop caller holds its single-instance lock and closes all application
writers before calling migrate. No method opens the real Windows registry.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.data_location_store import DataLocationStore, PendingCleanup
from app.data_roots import (
    MARKER_NAME,
    DataRootMarker,
    DataRootReference,
    DataRootValidationError,
    _directory_identity,
    _reject_reparse_components,
    _reject_reparse_tree,
    read_data_root_marker,
    remove_owned_data_root,
    validate_existing_data_root,
    validate_new_data_root,
    write_data_root_marker,
    write_staged_data_root_marker,
)
from app.migration_recovery import ARCHIVE_NAME, RecoverySnapshotService
from app.paths import AppPaths, resolve_app_paths
from app.windows_owned_delete import delete_tree_bound


class DataMigrationError(ValueError):
    """A stable code containing no underlying exception or private data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MigrationEstimate:
    target_root: Path
    copy_bytes: int
    recovery_bytes: int
    free_bytes: int


@dataclass(frozen=True)
class MigrationResult:
    reference: DataRootReference
    original_removed: bool
    pending_cleanup: PendingCleanup | None


def _readonly_database(path: Path):
    return sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)


def _database_bytes(path: Path) -> int:
    with closing(_readonly_database(path)) as db:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        return pages * page_size


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(_readonly_database(source)) as original,
        closing(sqlite3.connect(destination)) as copied,
    ):
        original.backup(copied)


def _check_database(path: Path) -> None:
    with closing(_readonly_database(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise DataMigrationError("database_invalid")


def _file_fingerprint(path: Path) -> tuple[int, str]:
    _reject_reparse_components(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise DataMigrationError("source_invalid")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _inventory(paths: AppPaths) -> tuple[list[Path], list[Path]]:
    _reject_reparse_components(paths.data_root)
    _reject_reparse_tree(paths.data_root)
    excluded = {
        Path(MARKER_NAME),
        Path("backups") / ARCHIVE_NAME,
        paths.database_path.relative_to(paths.data_root),
    }
    excluded.update(
        Path(str(paths.database_path.relative_to(paths.data_root)) + suffix)
        for suffix in ("-wal", "-shm", "-journal")
    )
    files, directories = [], []
    for item in paths.data_root.rglob("*"):
        relative = item.relative_to(paths.data_root)
        if relative in excluded:
            continue
        details = item.lstat()
        if stat.S_ISDIR(details.st_mode):
            directories.append(relative)
        elif stat.S_ISREG(details.st_mode):
            files.append(relative)
        else:
            raise DataMigrationError("source_invalid")
    return sorted(files), sorted(directories)


class DataMigrationService:
    def __init__(
        self,
        store: DataLocationStore,
        *,
        resource_root: Path,
        destination_validator: Callable[[AppPaths], None],
        recovery_service: RecoverySnapshotService | None = None,
    ) -> None:
        self._store = store
        self._resource_root = resource_root
        self._destination_validator = destination_validator
        self._recovery = recovery_service or RecoverySnapshotService()

    def _paths(self, root: Path) -> AppPaths:
        return resolve_app_paths(
            frozen=True, bundle_root=self._resource_root, data_root=root
        )

    def estimate(
        self,
        source: DataRootReference,
        target_parent: Path,
        *,
        create_recovery: bool = True,
    ) -> MigrationEstimate:
        try:
            paths = validate_existing_data_root(
                source, resource_root=self._resource_root, require_writable=False
            )
            target = validate_new_data_root(
                target_parent,
                source_root=paths.data_root,
                install_root=self._resource_root,
            )
            files, _ = _inventory(paths)
            if not paths.key_path.is_file() or not paths.key_path.stat().st_size:
                raise DataMigrationError("source_invalid")
            database_bytes = _database_bytes(paths.database_path)
            copy_bytes = database_bytes + sum(
                (paths.data_root / name).stat().st_size for name in files
            )
            # Conservative upper bound for the compressed archive plus its
            # temporary consistent database, manifest and ZIP overhead.
            critical = database_bytes + paths.key_path.stat().st_size
            recovery_bytes = database_bytes + critical + critical // 1000 + 65536
            return MigrationEstimate(
                target,
                copy_bytes,
                recovery_bytes if create_recovery else 0,
                shutil.disk_usage(target.parent).free,
            )
        except DataMigrationError:
            raise
        except DataRootValidationError as exc:
            raise DataMigrationError(exc.code) from None
        except (OSError, ValueError, sqlite3.Error):
            raise DataMigrationError("source_invalid") from None

    def _copy(self, source: AppPaths, destination: AppPaths) -> None:
        files, directories = _inventory(source)
        for relative in directories:
            (destination.data_root / relative).mkdir(parents=True, exist_ok=True)
        for relative in files:
            original = source.data_root / relative
            copied = destination.data_root / relative
            before = _file_fingerprint(original)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, copied, follow_symlinks=False)
            if before != _file_fingerprint(copied) or before != _file_fingerprint(
                original
            ):
                raise DataMigrationError("file_verification_failed")
        if _inventory(source) != (files, directories):
            raise DataMigrationError("file_verification_failed")

    def _discard_staging(self, staging: Path, identity: tuple[int, int]) -> None:
        _reject_reparse_components(staging)
        if _directory_identity(staging) != identity:
            raise DataMigrationError("cleanup_failed")
        _reject_reparse_tree(staging)
        delete_tree_bound(staging, identity)

    def _quarantine(self, root: Path, identity: tuple[int, int]) -> None:
        _reject_reparse_components(root)
        if _directory_identity(root) != identity:
            raise DataMigrationError("cleanup_failed")
        quarantine = root.with_name(f".eve-income-tracker-failed-{uuid4().hex}")
        try:
            os.replace(root, quarantine)
        except OSError:
            # If rename is blocked, make the formal root unselectable in place.
            marker = read_data_root_marker(root)
            write_data_root_marker(
                root, DataRootMarker(marker.reference, marker.created_at, "incomplete")
            )

    def migrate(
        self,
        source: DataRootReference,
        target_parent: Path,
        *,
        create_recovery: bool = True,
    ) -> MigrationResult:
        estimate = self.estimate(source, target_parent, create_recovery=create_recovery)
        try:
            if self._store.read_current() != source:
                raise DataMigrationError("source_not_current")
            if self._store.read_pending_cleanup() is not None:
                raise DataMigrationError("cleanup_pending")
        except DataMigrationError:
            raise
        except (OSError, ValueError, sqlite3.Error, RuntimeError):
            raise DataMigrationError("registry_failed") from None
        # Reserve small marker/SQLite filesystem overhead even without recovery.
        if estimate.free_bytes < estimate.copy_bytes + estimate.recovery_bytes + 65536:
            raise DataMigrationError("space_insufficient")
        target = estimate.target_root
        staging = target.with_name(f".eve-income-tracker-migrating-{uuid4().hex}")
        reference = DataRootReference(target, uuid4().hex, 1)
        created = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        identity = None
        renamed = False
        switch_attempted = False
        finalize_attempted = False
        pending_written = False
        phase = "staging_failed"
        try:
            staging.mkdir()
            identity = _directory_identity(staging)
            phase = "marker_failed"
            write_staged_data_root_marker(
                staging, DataRootMarker(reference, created, "incomplete")
            )
            destination = self._paths(staging)
            source_paths = self._paths(source.root)
            phase = "copy_failed"
            self._copy(source_paths, destination)
            phase = "database_invalid"
            _backup_database(source_paths.database_path, destination.database_path)
            _check_database(destination.database_path)
            phase = "destination_invalid"
            self._destination_validator(destination)
            phase = "marker_failed"
            write_staged_data_root_marker(
                staging, DataRootMarker(reference, created, "ready")
            )
            if create_recovery:
                phase = "recovery_failed"
                # Read the already validated destination: never recopy an old
                # archive or reopen the original DB for this snapshot.
                self._recovery.create(destination, destination)
            phase = "rename_failed"
            validate_new_data_root(
                target.parent, source_root=source.root, install_root=self._resource_root
            )
            _reject_reparse_components(staging)
            if _directory_identity(staging) != identity:
                raise DataMigrationError("staging_changed")
            _reject_reparse_tree(staging)
            if target.exists():
                target.rmdir()  # Only an empty, freshly revalidated target.
            os.rename(staging, target)
            renamed = True
            validate_existing_data_root(reference, resource_root=self._resource_root)
            phase = "registry_failed"
            switch_attempted = True
            self._store.begin_current_switch(reference)
            phase = "destination_reopen_failed"
            self._destination_validator(self._paths(target))
            phase = "registry_failed"
            # Persist the cleanup obligation before committing so a crash cannot
            # silently orphan the old root. No deletion happens before finalize.
            pending_written = True
            self._store.write_pending_cleanup(PendingCleanup(source))
            finalize_attempted = True
            self._store.finalize_current_switch()
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            if finalize_attempted:
                try:
                    committed = self._store.read_current() == reference
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    raise DataMigrationError("registry_state_unknown") from None
                if committed:
                    # The Task 2 commit fence is authoritative even if journal
                    # cleanup failed. Never quarantine the selected good root.
                    raise DataMigrationError("registry_finalize_failed") from None
            rollback_failed = False
            if switch_attempted:
                try:
                    self._store.rollback_current_switch()
                    if pending_written:
                        self._store.clear_pending_cleanup()
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    rollback_failed = True
                try:
                    still_source = self._store.read_current() == source
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    raise DataMigrationError("registry_state_unknown") from None
                if not still_source:
                    raise DataMigrationError("registry_rollback_failed") from None
            if identity is not None:
                try:
                    if renamed:
                        self._quarantine(target, identity)
                    else:
                        self._discard_staging(staging, identity)
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    raise DataMigrationError("cleanup_failed") from None
            if rollback_failed:
                raise DataMigrationError("registry_rollback_failed") from None
            if isinstance(exc, DataMigrationError):
                raise
            raise DataMigrationError(phase) from None

        try:
            self._store.clear_pending_migration()
        except (OSError, ValueError, sqlite3.Error, RuntimeError):
            raise DataMigrationError("cleanup_state_failed") from None
        try:
            remove_owned_data_root(source)
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            if (
                isinstance(exc, DataRootValidationError)
                and exc.code == "remove_restore_failed"
            ):
                try:
                    self._store.clear_pending_cleanup()
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    raise DataMigrationError("cleanup_state_failed") from None
                raise DataMigrationError("cleanup_restore_failed") from None
            return MigrationResult(reference, False, PendingCleanup(source))
        try:
            self._store.clear_pending_cleanup()
        except (OSError, ValueError, sqlite3.Error, RuntimeError):
            raise DataMigrationError("cleanup_state_failed") from None
        return MigrationResult(reference, True, None)

    def retry_pending_cleanup(self) -> bool:
        try:
            pending = self._store.read_pending_cleanup()
            if pending is None:
                return True
            current = self._store.read_current()
            if (
                current is None
                or current.root.resolve() == pending.reference.root.resolve()
            ):
                return False
            # Validate the selected root too: a corrupt pointer must not authorize
            # deletion of what might be the only surviving copy.
            validate_existing_data_root(
                current, resource_root=self._resource_root, require_writable=False
            )
            remove_owned_data_root(pending.reference)
            self._store.clear_pending_cleanup()
            return True
        except DataRootValidationError as exc:
            if exc.code == "remove_restore_failed":
                try:
                    self._store.clear_pending_cleanup()
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    return False
            return False
        except (OSError, ValueError, sqlite3.Error, RuntimeError):
            return False
