from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.storage import LATEST_SCHEMA_VERSION, Database

_REQUIRED_SCHEMA = {
    1: {
        "characters": {
            "character_id",
            "name",
            "encrypted_refresh_token",
            "enabled",
            "authorization_required",
            "tracking_started_at",
            "last_synced_at",
        },
        "wallet_journal": {
            "character_id",
            "journal_id",
            "date_utc",
            "amount",
            "ref_type",
            "description",
        },
    },
    2: {
        "character_groups": {"group_id", "name", "created_at"},
        "characters": {
            "character_id",
            "name",
            "encrypted_refresh_token",
            "enabled",
            "authorization_required",
            "tracking_started_at",
            "last_synced_at",
            "group_id",
        },
        "wallet_journal": {
            "character_id",
            "journal_id",
            "date_utc",
            "amount",
            "ref_type",
            "description",
        },
    },
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DatabaseLifecycleError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(frozen=True)
class ImportResult:
    source_version: int
    characters_imported: int
    journal_entries_imported: int


@dataclass(frozen=True)
class AuthorizationResetResult:
    character_count: int
    backup_path: Path


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _schema_is_supported(path: Path, version: int) -> bool:
    required = _REQUIRED_SCHEMA.get(version)
    if required is None:
        return False
    connection = _read_only_connection(path)
    try:
        for table, expected_columns in required.items():
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            if not expected_columns.issubset({row[1] for row in rows}):
                return False
        return True
    finally:
        connection.close()


def _is_locked(error: sqlite3.Error) -> bool:
    rendered = str(error).lower()
    return "locked" in rendered or "busy" in rendered


def _remove_sqlite_temporary_files(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_to_temporary(source: Path, directory: Path, prefix: str) -> Path:
    fd = -1
    temporary_path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix=prefix,
            suffix=".tmp",
            dir=directory,
        )
        temporary_path = Path(raw_path)
        output = os.fdopen(fd, "wb")
        fd = -1
        with source.open("rb") as input_file, output:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        return temporary_path
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if temporary_path is not None:
            _remove_sqlite_temporary_files(temporary_path)
        raise


def _snapshot_sqlite_to_temporary(
    source: Path,
    directory: Path,
    prefix: str,
) -> Path:
    fd = -1
    temporary_path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix=prefix,
            suffix=".tmp",
            dir=directory,
        )
        temporary_path = Path(raw_path)
        os.close(fd)
        fd = -1
        source_connection = _read_only_connection(source)
        try:
            destination_connection = sqlite3.connect(temporary_path)
            try:
                source_connection.backup(destination_connection)
                destination_connection.commit()
            finally:
                destination_connection.close()
        finally:
            source_connection.close()
        _flush_file(temporary_path)
        return temporary_path
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if temporary_path is not None:
            _remove_sqlite_temporary_files(temporary_path)
        raise


def _flush_file(path: Path) -> None:
    with path.open("r+b") as file:
        os.fsync(file.fileno())


class DatabaseLifecycle:
    def __init__(
        self,
        database: Database,
        backups_dir: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.database = database
        self.backups_dir = Path(backups_dir)
        self._clock = clock

    def prepare(self) -> None:
        path = self.database.path
        try:
            exists = path.exists()
        except OSError:
            raise DatabaseLifecycleError("database_unreadable") from None

        if not exists:
            self._create_new_database()
            return

        version = self._inspect_target()
        if version == LATEST_SCHEMA_VERSION:
            return

        backup = self._create_backup(version)
        try:
            self.database.initialize()
            migrated_version = self.database.inspect_version()
            if migrated_version != LATEST_SCHEMA_VERSION or not _schema_is_supported(
                path, migrated_version
            ):
                raise sqlite3.DatabaseError("migration validation failed")
        except Exception:  # noqa: BLE001 - restore and sanitize migration failures
            try:
                self._restore_backup(backup)
            except OSError:
                raise DatabaseLifecycleError("database_restore_failed") from None
            raise DatabaseLifecycleError("migration_failed") from None

        self._prune_backups()

    def import_legacy(self, source: Path) -> ImportResult:
        source_path = Path(source)
        target_path = self.database.path

        try:
            same_database = source_path.resolve() == target_path.resolve()
        except OSError:
            same_database = False
        if same_database:
            raise DatabaseLifecycleError("import_same_database")

        try:
            if target_path.exists():
                raise DatabaseLifecycleError("import_target_exists")
        except OSError:
            raise DatabaseLifecycleError("import_failed") from None

        source_version = self._inspect_import_source(source_path)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = _snapshot_sqlite_to_temporary(
                source_path,
                target_path.parent,
                f".{target_path.name}.import-",
            )
        except (OSError, sqlite3.Error):
            raise DatabaseLifecycleError("import_failed") from None

        try:
            temporary_database = Database(temporary_path)
            if source_version < LATEST_SCHEMA_VERSION:
                temporary_database.initialize()
            imported_version = temporary_database.inspect_version()
            if imported_version != LATEST_SCHEMA_VERSION or not _schema_is_supported(
                temporary_path, imported_version
            ):
                raise sqlite3.DatabaseError("import validation failed")
            character_count, journal_count = self._invalidate_credentials(
                temporary_path
            )
            _flush_file(temporary_path)
            try:
                os.link(temporary_path, target_path)
            except FileExistsError:
                raise DatabaseLifecycleError("import_target_exists") from None
            return ImportResult(
                source_version=source_version,
                characters_imported=character_count,
                journal_entries_imported=journal_count,
            )
        except DatabaseLifecycleError:
            raise
        except Exception:  # noqa: BLE001 - sanitize copied database failures
            raise DatabaseLifecycleError("import_failed") from None
        finally:
            _remove_sqlite_temporary_files(temporary_path)

    def reset_authorization(self) -> AuthorizationResetResult:
        version = self._inspect_target()
        if version != LATEST_SCHEMA_VERSION:
            raise DatabaseLifecycleError("authorization_reset_failed")

        backup = self._create_backup(version)
        try:
            character_count, _ = self._invalidate_credentials(self.database.path)
            if (
                self.database.inspect_version() != LATEST_SCHEMA_VERSION
                or not _schema_is_supported(
                    self.database.path,
                    LATEST_SCHEMA_VERSION,
                )
            ):
                raise sqlite3.DatabaseError("authorization reset validation failed")
        except Exception:  # noqa: BLE001 - restore and sanitize reset failures.
            try:
                self._restore_backup(backup)
            except OSError:
                raise DatabaseLifecycleError("database_restore_failed") from None
            raise DatabaseLifecycleError("authorization_reset_failed") from None

        self._prune_backups()
        return AuthorizationResetResult(character_count, backup)

    def rollback_authorization_reset(
        self,
        reset: AuthorizationResetResult,
    ) -> None:
        try:
            self._restore_backup(reset.backup_path)
            if (
                self.database.inspect_version() != LATEST_SCHEMA_VERSION
                or not _schema_is_supported(
                    self.database.path,
                    LATEST_SCHEMA_VERSION,
                )
            ):
                raise sqlite3.DatabaseError("authorization rollback validation failed")
        except Exception:  # noqa: BLE001 - restore and sanitize rollback failures.
            raise DatabaseLifecycleError("database_restore_failed") from None

    def _inspect_target(self) -> int:
        try:
            version = self.database.inspect_version()
            if version > LATEST_SCHEMA_VERSION:
                raise DatabaseLifecycleError("database_future")
            if version < 1 or not _schema_is_supported(self.database.path, version):
                raise DatabaseLifecycleError("database_corrupt")
            return version
        except DatabaseLifecycleError:
            raise
        except sqlite3.OperationalError as error:
            code = "database_locked" if _is_locked(error) else "database_corrupt"
            raise DatabaseLifecycleError(code) from None
        except (OSError, sqlite3.DatabaseError):
            raise DatabaseLifecycleError("database_corrupt") from None

    @staticmethod
    def _inspect_import_source(source: Path) -> int:
        try:
            version = Database(source).inspect_version()
            if version > LATEST_SCHEMA_VERSION:
                raise DatabaseLifecycleError("import_future")
            if version < 1 or not _schema_is_supported(source, version):
                raise DatabaseLifecycleError("import_invalid")
            return version
        except DatabaseLifecycleError:
            raise
        except (OSError, sqlite3.DatabaseError):
            raise DatabaseLifecycleError("import_invalid") from None

    def _create_new_database(self) -> None:
        path = self.database.path
        temporary_path: Path | None = None
        fd = -1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_path = tempfile.mkstemp(
                prefix=f".{path.name}.create-",
                suffix=".tmp",
                dir=path.parent,
            )
            os.close(fd)
            fd = -1
            temporary_path = Path(raw_path)
            temporary_database = Database(temporary_path)
            temporary_database.initialize()
            if (
                temporary_database.inspect_version() != LATEST_SCHEMA_VERSION
                or not _schema_is_supported(temporary_path, LATEST_SCHEMA_VERSION)
            ):
                raise sqlite3.DatabaseError("new database validation failed")
            _flush_file(temporary_path)
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                self.prepare()
        except DatabaseLifecycleError:
            raise
        except Exception:  # noqa: BLE001 - sanitize filesystem and SQLite failures
            raise DatabaseLifecycleError("database_unwritable") from None
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary_path is not None:
                _remove_sqlite_temporary_files(temporary_path)

    def _create_backup(self, version: int) -> Path:
        timestamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.backups_dir / f"eve_income-v{version}-{timestamp}.sqlite3"
        temporary_path: Path | None = None
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            temporary_path = _copy_to_temporary(
                self.database.path,
                self.backups_dir,
                f".{backup_path.name}.",
            )
            os.link(temporary_path, backup_path)
            return backup_path
        except OSError:
            raise DatabaseLifecycleError("backup_unwritable") from None
        finally:
            if temporary_path is not None:
                _remove_sqlite_temporary_files(temporary_path)

    def _restore_backup(self, backup: Path) -> None:
        temporary_path = _copy_to_temporary(
            backup,
            self.database.path.parent,
            f".{self.database.path.name}.restore-",
        )
        try:
            os.replace(temporary_path, self.database.path)
        finally:
            _remove_sqlite_temporary_files(temporary_path)

    def _prune_backups(self) -> None:
        try:
            backups = sorted(
                candidate
                for candidate in self.backups_dir.glob("eve_income-v*-*.sqlite3")
                if candidate.is_file()
            )
        except OSError:
            return
        for old_backup in backups[:-3]:
            try:
                old_backup.unlink()
            except OSError:
                pass

    @staticmethod
    def _invalidate_credentials(path: Path) -> tuple[int, int]:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            character_count = int(
                connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
            )
            journal_count = int(
                connection.execute("SELECT COUNT(*) FROM wallet_journal").fetchone()[0]
            )
            connection.execute(
                "UPDATE characters SET encrypted_refresh_token = '', "
                "authorization_required = 1"
            )
            connection.commit()
            return character_count, journal_count
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
