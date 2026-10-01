"""Short-lived, single-file recovery snapshots for a completed data migration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from app.paths import AppPaths

ARCHIVE_NAME = "migration-recovery.zip"
MANIFEST_NAME = "recovery-manifest.json"
DATABASE_NAME = "data/eve_income.sqlite3"
KEY_NAME = "config/local.key"
_MANIFEST_FIELDS = frozenset(
    {"schema", "created_at", "successful_starts", "database_sha256", "key_sha256"}
)
_ENTRIES = frozenset({DATABASE_NAME, KEY_NAME, MANIFEST_NAME})
_RETENTION_AGE = timedelta(days=7)


class RecoverySnapshotError(ValueError):
    """A stable, path-free reason for refusing a recovery operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RecoverySnapshotInfo:
    archive_path: Path
    created_at: datetime
    size_bytes: int
    successful_starts: int


def _temporary_file(directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    os.close(descriptor)
    return Path(name)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_bytes(
    created_at: datetime, successful_starts: int, database_hash: str, key_hash: str
) -> bytes:
    payload = {
        "schema": 1,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "successful_starts": successful_starts,
        "database_sha256": database_hash,
        "key_sha256": key_hash,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _member_hash(archive: ZipFile, name: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(name) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class RecoverySnapshotService:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        try:
            value = self._clock()
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError
            if value.utcoffset() is None:
                raise ValueError
            return value.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            raise RecoverySnapshotError("clock_invalid") from None

    def _validate_archive(self, path: Path) -> tuple[datetime, int]:
        try:
            with ZipFile(path) as archive:
                names = archive.namelist()
                if len(names) != 3 or set(names) != _ENTRIES:
                    raise ValueError
                if any(
                    archive.getinfo(name).is_dir()
                    or archive.getinfo(name).compress_type != ZIP_DEFLATED
                    for name in names
                ):
                    raise ValueError
                manifest_bytes = archive.read(MANIFEST_NAME)
                if len(manifest_bytes) > 4096:
                    raise ValueError
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
                    raise ValueError
                if type(manifest["schema"]) is not int or manifest["schema"] != 1:
                    raise ValueError
                starts = manifest["successful_starts"]
                if type(starts) is not int or starts < 0:
                    raise ValueError
                stamp = manifest["created_at"]
                if not isinstance(stamp, str) or not stamp.endswith("Z"):
                    raise ValueError
                created = datetime.fromisoformat(stamp)
                if (
                    created.utcoffset() != timedelta(0)
                    or created.isoformat().replace("+00:00", "Z") != stamp
                ):
                    raise ValueError
                for name, field in (
                    (DATABASE_NAME, "database_sha256"),
                    (KEY_NAME, "key_sha256"),
                ):
                    expected = manifest[field]
                    if (
                        not isinstance(expected, str)
                        or len(expected) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in expected
                        )
                    ):
                        raise ValueError
                    actual, size = _member_hash(archive, name)
                    if not size or actual != expected:
                        raise ValueError
            return created, starts
        except (OSError, BadZipFile, EOFError, KeyError, UnicodeError, ValueError):
            raise RecoverySnapshotError("snapshot_invalid") from None

    def inspect(self, paths: AppPaths) -> RecoverySnapshotInfo | None:
        archive_path = paths.backups_dir / ARCHIVE_NAME
        try:
            if not archive_path.exists():
                return None
            if not archive_path.is_file() or archive_path.is_symlink():
                raise RecoverySnapshotError("snapshot_invalid")
            created, starts = self._validate_archive(archive_path)
            return RecoverySnapshotInfo(
                archive_path, created, archive_path.stat().st_size, starts
            )
        except OSError:
            raise RecoverySnapshotError("snapshot_invalid") from None

    def create(self, source: AppPaths, destination: AppPaths) -> RecoverySnapshotInfo:
        archive_path = destination.backups_dir / ARCHIVE_NAME
        database_temp: Path | None = None
        archive_temp: Path | None = None
        try:
            destination.backups_dir.mkdir(parents=True, exist_ok=True)
            database_temp = _temporary_file(destination.backups_dir, "recovery-db-")
            try:
                source_uri = (
                    source.database_path.resolve(strict=True).as_uri() + "?mode=ro"
                )
                with (
                    closing(sqlite3.connect(source_uri, uri=True)) as source_db,
                    closing(sqlite3.connect(database_temp)) as snapshot_db,
                ):
                    source_db.backup(snapshot_db)
                    if snapshot_db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise RecoverySnapshotError("source_invalid")
                key = source.key_path.read_bytes()
                if not key:
                    raise RecoverySnapshotError("source_invalid")
            except (OSError, sqlite3.Error, ValueError):
                raise RecoverySnapshotError("source_invalid") from None
            created = self._now()
            manifest = _manifest_bytes(
                created, 0, _sha256_file(database_temp), hashlib.sha256(key).hexdigest()
            )
            archive_temp = _temporary_file(destination.backups_dir, "recovery-archive-")
            with ZipFile(archive_temp, "w", compression=ZIP_DEFLATED) as archive:
                archive.write(database_temp, DATABASE_NAME)
                archive.writestr(KEY_NAME, key)
                archive.writestr(MANIFEST_NAME, manifest)
            self._validate_archive(archive_temp)
            os.replace(archive_temp, archive_path)
            archive_temp = None
            return self.inspect(destination)
        except RecoverySnapshotError:
            raise
        except (OSError, sqlite3.Error, BadZipFile, ValueError):
            raise RecoverySnapshotError("snapshot_write_failed") from None
        finally:
            if archive_temp is not None:
                archive_temp.unlink(missing_ok=True)
            if database_temp is not None:
                database_temp.unlink(missing_ok=True)

    def record_success_and_prune(self, paths: AppPaths) -> RecoverySnapshotInfo | None:
        info = self.inspect(paths)
        if info is None:
            return None
        starts = info.successful_starts + 1
        if starts >= 3 and self._now() - info.created_at >= _RETENTION_AGE:
            self.delete(paths)
            return None
        temporary: Path | None = None
        try:
            temporary = _temporary_file(paths.backups_dir, "recovery-update-")
            with (
                ZipFile(info.archive_path) as old,
                ZipFile(temporary, "w", compression=ZIP_DEFLATED) as updated,
            ):
                for name in (DATABASE_NAME, KEY_NAME):
                    with (
                        old.open(name) as source_stream,
                        updated.open(name, "w") as target,
                    ):
                        shutil.copyfileobj(source_stream, target, 1024 * 1024)
                previous = json.loads(old.read(MANIFEST_NAME))
                updated.writestr(
                    MANIFEST_NAME,
                    _manifest_bytes(
                        info.created_at,
                        starts,
                        previous["database_sha256"],
                        previous["key_sha256"],
                    ),
                )
            self._validate_archive(temporary)
            os.replace(temporary, info.archive_path)
            temporary = None
            return self.inspect(paths)
        except RecoverySnapshotError:
            raise
        except (OSError, BadZipFile, EOFError, ValueError):
            raise RecoverySnapshotError("snapshot_write_failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self, paths: AppPaths) -> bool:
        try:
            (paths.backups_dir / ARCHIVE_NAME).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise RecoverySnapshotError("snapshot_delete_failed") from None
