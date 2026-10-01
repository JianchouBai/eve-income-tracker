"""Owned application data roots and their destructive-operation boundary."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.paths import APP_DATA_DIRECTORY, PROJECT_ROOT, AppPaths, resolve_app_paths

MARKER_NAME = ".eve-income-tracker-data.json"
APP_ID = "59D95F12-7D3B-4C8A-A57D-98F836769441"
DATA_ROOT_SCHEMA = 1
_MARKER_FIELDS = frozenset(
    {"app_id", "created_at", "root", "root_id", "schema", "state"}
)
_STATES = frozenset({"incomplete", "ready"})


class DataRootValidationError(ValueError):
    """A stable, path-free reason for refusing a data-root operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DataRootReference:
    root: Path
    root_id: str
    schema: int


@dataclass(frozen=True)
class DataRootMarker:
    reference: DataRootReference
    created_at: str
    state: str


def _canonical(path: Path, *, reject_traversal: bool = False) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise DataRootValidationError("path_invalid")
    if reject_traversal and ".." in path.parts:
        raise DataRootValidationError("path_invalid")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise DataRootValidationError("path_invalid") from None


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise DataRootValidationError("path_unavailable") from None
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _reject_reparse_components(path: Path) -> None:
    for component in (path, *path.parents):
        if _is_reparse(component):
            raise DataRootValidationError("path_reparse")


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _protected_directories() -> tuple[Path, ...]:
    candidates = [
        PROJECT_ROOT,
        Path(sys.executable).resolve().parent,
    ]
    for name, fallback in (
        ("WINDIR", "C:\\Windows"),
        ("ProgramFiles", "C:\\Program Files"),
        ("ProgramFiles(x86)", "C:\\Program Files (x86)"),
    ):
        candidates.append(Path(os.environ.get(name, fallback)))
    return tuple(
        path.resolve(strict=False) for path in candidates if path.is_absolute()
    )


def _reject_unsafe_root(root: Path, *additional_protected: Path) -> None:
    if (
        root == Path(root.anchor)
        or root == Path.home().resolve(strict=False)
        or root.name != APP_DATA_DIRECTORY
    ):
        raise DataRootValidationError("path_unsafe")
    protected_directories = (*_protected_directories(), *additional_protected)
    if any(
        _inside(root, protected) or _inside(protected, root)
        for protected in protected_directories
    ):
        raise DataRootValidationError("path_unsafe")


def _validate_reference(reference: DataRootReference) -> Path:
    if not isinstance(reference, DataRootReference):
        raise DataRootValidationError("reference_invalid")
    root = _canonical(reference.root, reject_traversal=True)
    if type(reference.schema) is not int or reference.schema != DATA_ROOT_SCHEMA:
        raise DataRootValidationError("schema_invalid")
    try:
        root_uuid = UUID(reference.root_id)
    except (TypeError, ValueError, AttributeError):
        raise DataRootValidationError("root_id_invalid") from None
    if root_uuid.version != 4:
        raise DataRootValidationError("root_id_invalid")
    _reject_reparse_components(reference.root)
    return root


def _validate_marker(root: Path, marker: DataRootMarker) -> None:
    if not isinstance(marker, DataRootMarker):
        raise DataRootValidationError("marker_invalid")
    if _validate_reference(marker.reference) != root:
        raise DataRootValidationError("marker_mismatch")
    if not isinstance(marker.state, str) or marker.state not in _STATES:
        raise DataRootValidationError("marker_invalid")
    if not isinstance(marker.created_at, str) or not marker.created_at.endswith("Z"):
        raise DataRootValidationError("marker_invalid")
    try:
        created = datetime.fromisoformat(marker.created_at)
    except ValueError:
        raise DataRootValidationError("marker_invalid") from None
    if created.utcoffset().total_seconds() != 0:
        raise DataRootValidationError("marker_invalid")


def data_root_from_parent(parent: Path) -> Path:
    return _canonical(parent) / APP_DATA_DIRECTORY


def write_data_root_marker(root: Path, marker: DataRootMarker) -> None:
    _reject_reparse_components(root)
    canonical_root = _canonical(root, reject_traversal=True)
    _validate_marker(canonical_root, marker)
    if not canonical_root.is_dir():
        raise DataRootValidationError("path_unavailable")
    payload = {
        "app_id": APP_ID,
        "created_at": marker.created_at,
        "root": str(canonical_root),
        "root_id": marker.reference.root_id,
        "schema": marker.reference.schema,
        "state": marker.state,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=canonical_root, prefix=f"{MARKER_NAME}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, canonical_root / MARKER_NAME)
    except OSError:
        raise DataRootValidationError("marker_write_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_data_root_marker_at(directory: Path, expected_root: Path) -> DataRootMarker:
    _reject_reparse_components(directory)
    canonical_directory = _canonical(directory, reject_traversal=True)
    canonical_root = _canonical(expected_root, reject_traversal=True)
    marker_path = canonical_directory / MARKER_NAME
    if _is_reparse(marker_path):
        raise DataRootValidationError("path_reparse")
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DataRootValidationError("marker_invalid") from None
    if not isinstance(payload, dict) or set(payload) != _MARKER_FIELDS:
        raise DataRootValidationError("marker_invalid")
    if payload["app_id"] != APP_ID or type(payload["schema"]) is not int:
        raise DataRootValidationError("marker_invalid")
    if not isinstance(payload["root"], str) or payload["root"] != str(canonical_root):
        raise DataRootValidationError("marker_mismatch")
    marker = DataRootMarker(
        DataRootReference(canonical_root, payload["root_id"], payload["schema"]),
        payload["created_at"],
        payload["state"],
    )
    _validate_marker(canonical_root, marker)
    return marker


def read_data_root_marker(root: Path) -> DataRootMarker:
    return _read_data_root_marker_at(root, root)


def validate_existing_data_root(
    reference: DataRootReference,
    *,
    resource_root: Path,
    require_writable: bool = True,
) -> AppPaths:
    root = _validate_reference(reference)
    _reject_unsafe_root(root, resource_root.resolve(strict=False))
    if not root.is_dir():
        raise DataRootValidationError("path_unavailable")
    marker = read_data_root_marker(root)
    if marker.reference != DataRootReference(root, reference.root_id, reference.schema):
        raise DataRootValidationError("marker_mismatch")
    if marker.state != "ready":
        raise DataRootValidationError("marker_incomplete")
    if require_writable:
        try:
            with tempfile.TemporaryFile(dir=root):
                pass
        except OSError:
            raise DataRootValidationError("path_not_writable") from None
    return resolve_app_paths(frozen=True, bundle_root=resource_root, data_root=root)


def validate_new_data_root(
    parent: Path, *, source_root: Path, install_root: Path
) -> Path:
    _reject_reparse_components(parent)
    canonical_parent = _canonical(parent, reject_traversal=True)
    source = _canonical(source_root, reject_traversal=True)
    install = _canonical(install_root, reject_traversal=True)
    root = data_root_from_parent(canonical_parent)
    _reject_reparse_components(root)
    _reject_unsafe_root(root)
    if canonical_parent == Path.home().resolve(strict=False):
        raise DataRootValidationError("path_unsafe")
    if not canonical_parent.is_dir():
        raise DataRootValidationError("path_unavailable")
    if any(
        _inside(root, forbidden)
        or _inside(forbidden, root)
        or _inside(canonical_parent, forbidden)
        for forbidden in (source, install)
    ):
        raise DataRootValidationError("path_unsafe")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise DataRootValidationError("target_not_empty")
    if not os.access(canonical_parent, os.W_OK):
        raise DataRootValidationError("path_not_writable")
    return root


def _reject_reparse_tree(root: Path) -> None:
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                details = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(details.st_mode) or (
                    getattr(details, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                ):
                    raise DataRootValidationError("path_reparse")
                if entry.is_dir(follow_symlinks=False):
                    _reject_reparse_tree(Path(entry.path))
    except OSError:
        raise DataRootValidationError("path_unavailable") from None


def _directory_identity(root: Path) -> tuple[int, int]:
    try:
        details = root.stat(follow_symlinks=False)
    except OSError:
        raise DataRootValidationError("path_unavailable") from None
    if not stat.S_ISDIR(details.st_mode) or not details.st_ino:
        raise DataRootValidationError("remove_identity_unavailable")
    return details.st_dev, details.st_ino


def _restore_quarantined_root(quarantine: Path, root: Path) -> None:
    try:
        if root.exists() or _is_reparse(root):
            raise DataRootValidationError("remove_restore_failed")
        os.rename(quarantine, root)
    except OSError:
        raise DataRootValidationError("remove_restore_failed") from None


def remove_owned_data_root(reference: DataRootReference) -> None:
    root = _validate_reference(reference)
    _reject_unsafe_root(root)
    if not root.is_dir():
        raise DataRootValidationError("path_unavailable")
    marker = read_data_root_marker(root)
    if marker.reference != DataRootReference(root, reference.root_id, reference.schema):
        raise DataRootValidationError("marker_mismatch")
    if marker.state != "ready":
        raise DataRootValidationError("marker_incomplete")
    _reject_reparse_tree(root)
    identity = _directory_identity(root)
    quarantine = root.with_name(f".eve-income-tracker-removing-{uuid4().hex}")
    if quarantine.exists() or _is_reparse(quarantine):
        raise DataRootValidationError("remove_failed")
    try:
        os.replace(root, quarantine)
    except OSError:
        raise DataRootValidationError("remove_failed") from None
    try:
        _reject_reparse_components(quarantine)
        if _directory_identity(quarantine) != identity:
            raise DataRootValidationError("remove_identity_changed")
        moved_marker = _read_data_root_marker_at(quarantine, root)
        if moved_marker != marker:
            raise DataRootValidationError("marker_mismatch")
        _reject_reparse_tree(quarantine)
    except DataRootValidationError:
        _restore_quarantined_root(quarantine, root)
        raise
    try:
        shutil.rmtree(quarantine)
    except OSError:
        raise DataRootValidationError("remove_failed") from None
