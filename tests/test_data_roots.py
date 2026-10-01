import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app import data_roots
from app.data_roots import (
    DataRootMarker,
    DataRootReference,
    DataRootValidationError,
    data_root_from_parent,
    read_data_root_marker,
    remove_owned_data_root,
    validate_existing_data_root,
    validate_new_data_root,
    write_data_root_marker,
)

APP_ID = "59D95F12-7D3B-4C8A-A57D-98F836769441"
CREATED_AT = "2026-10-01T00:00:00Z"


def reference(root: Path) -> DataRootReference:
    return DataRootReference(root, uuid4().hex, 1)


def marker_for(root: Path, state: str = "ready") -> DataRootMarker:
    return DataRootMarker(reference(root), CREATED_AT, state)


def test_parent_selection_appends_fixed_child_and_normalizes_absolute_path(tmp_path):
    parent = tmp_path / "selected" / ".." / "selected"

    assert (
        data_root_from_parent(parent)
        == (tmp_path / "selected" / "EVE Income Tracker").resolve()
    )


def test_disk_root_parent_produces_a_child_data_root(tmp_path):
    disk_root = Path(tmp_path.anchor)

    assert data_root_from_parent(disk_root) == disk_root / "EVE Income Tracker"


def test_marker_is_canonical_utf8_json_and_round_trips(tmp_path):
    root = tmp_path / "数据" / "EVE Income Tracker"
    root.mkdir(parents=True)
    marker = marker_for(root)

    write_data_root_marker(root, marker)

    marker_path = root / ".eve-income-tracker-data.json"
    expected = {
        "app_id": APP_ID,
        "created_at": CREATED_AT,
        "root": str(root.resolve()),
        "root_id": marker.reference.root_id,
        "schema": 1,
        "state": "ready",
    }
    assert marker_path.read_bytes() == (
        json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    assert read_data_root_marker(root) == marker
    assert list(root.iterdir()) == [marker_path]


@pytest.mark.parametrize("state", ["ready", "incomplete"])
def test_marker_accepts_only_expected_states(tmp_path, state):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root, state)

    write_data_root_marker(root, marker)

    assert read_data_root_marker(root) == marker


@pytest.mark.parametrize("invalid", ["schema", "state", "id"])
def test_marker_rejects_invalid_fields_before_write(tmp_path, invalid):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    if invalid == "schema":
        marker = DataRootMarker(
            DataRootReference(root, marker.reference.root_id, 2), CREATED_AT, "ready"
        )
    elif invalid == "state":
        marker = DataRootMarker(marker.reference, CREATED_AT, "unknown")
    else:
        marker = DataRootMarker(
            DataRootReference(root, "guessable", 1), CREATED_AT, "ready"
        )

    with pytest.raises(DataRootValidationError):
        write_data_root_marker(root, marker)

    assert not (root / ".eve-income-tracker-data.json").exists()


@pytest.mark.parametrize("corruption", ["json", "app", "root", "extra", "state-type"])
def test_read_marker_rejects_corruption(tmp_path, corruption):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    write_data_root_marker(root, marker_for(root))
    marker_path = root / ".eve-income-tracker-data.json"
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    if corruption == "json":
        marker_path.write_text("{", encoding="utf-8")
    else:
        if corruption == "app":
            payload["app_id"] = "another application"
        elif corruption == "root":
            payload["root"] = str(tmp_path / "another root")
        elif corruption == "state-type":
            payload["state"] = ["ready"]
        else:
            payload["unexpected"] = "value"
        marker_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DataRootValidationError):
        read_data_root_marker(root)


def test_existing_root_requires_matching_ready_marker_and_builds_paths(tmp_path):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    bundle = tmp_path / "program"

    paths = validate_existing_data_root(marker.reference, resource_root=bundle)

    assert paths.resource_root == bundle
    assert paths.data_root == root.resolve()
    assert paths.database_path == root / "data" / "eve_income.sqlite3"
    assert paths.key_path == root / "config" / "local.key"
    assert paths.logs_dir == root / "logs"
    assert paths.backups_dir == root / "backups"
    assert paths.webview_storage_dir == root / "config" / "webview"


def test_existing_default_root_under_user_profile_is_allowed(tmp_path, monkeypatch):
    user = tmp_path / "user"
    root = user / "AppData" / "Local" / "EVE Income Tracker"
    root.mkdir(parents=True)
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))

    paths = validate_existing_data_root(
        marker.reference, resource_root=tmp_path / "program"
    )

    assert paths.data_root == root


def test_existing_root_rejects_install_resource_nested_inside_it(tmp_path):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    nested_program = root / "program"
    nested_program.mkdir()

    with pytest.raises(DataRootValidationError) as caught:
        validate_existing_data_root(marker.reference, resource_root=nested_program)

    assert caught.value.code == "path_unsafe"


@pytest.mark.parametrize("mismatch", ["id", "schema", "incomplete"])
def test_existing_root_rejects_mismatched_or_incomplete_marker(tmp_path, mismatch):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root, "incomplete" if mismatch == "incomplete" else "ready")
    write_data_root_marker(root, marker)
    requested = marker.reference
    if mismatch == "id":
        requested = DataRootReference(root, uuid4().hex, 1)
    elif mismatch == "schema":
        requested = DataRootReference(root, requested.root_id, 2)

    with pytest.raises(DataRootValidationError):
        validate_existing_data_root(requested, resource_root=tmp_path / "program")


def test_new_root_accepts_empty_target_without_creating_it(tmp_path):
    parent = tmp_path / "chosen"
    parent.mkdir()
    root = validate_new_data_root(
        parent,
        source_root=tmp_path / "source" / "EVE Income Tracker",
        install_root=tmp_path / "program",
    )

    assert root == parent / "EVE Income Tracker"
    assert not root.exists()


@pytest.mark.parametrize(
    "danger", ["user-root", "windows", "program-files", "install", "source"]
)
def test_new_root_rejects_dangerous_parent(tmp_path, danger):
    source = tmp_path / "source" / "EVE Income Tracker"
    install = tmp_path / "program"
    choices = {
        "user-root": Path.home(),
        "windows": Path(os.environ.get("WINDIR", "C:\\Windows")),
        "program-files": Path(os.environ.get("ProgramFiles", "C:\\Program Files")),
        "install": install,
        "source": source,
    }

    with pytest.raises(DataRootValidationError):
        validate_new_data_root(
            choices[danger], source_root=source, install_root=install
        )


def test_remove_owned_root_rejects_disk_root_even_with_matching_marker(tmp_path):
    root = Path(tmp_path.anchor)
    with pytest.raises(DataRootValidationError):
        remove_owned_data_root(DataRootReference(root, uuid4().hex, 1))


def test_remove_owned_root_rejects_user_home_even_with_matching_marker(
    tmp_path, monkeypatch
):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))

    with pytest.raises(DataRootValidationError):
        remove_owned_data_root(marker.reference)

    assert root.exists()


def test_remove_owned_root_rejects_source_nested_inside_it(tmp_path, monkeypatch):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    source = root / "source"
    source.mkdir()
    private = source / "keep.txt"
    private.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(data_roots, "PROJECT_ROOT", source)

    with pytest.raises(DataRootValidationError) as caught:
        remove_owned_data_root(marker.reference)

    assert caught.value.code == "path_unsafe"
    assert private.read_text(encoding="utf-8") == "keep"


def test_remove_owned_root_revalidates_directory_moved_to_quarantine(
    tmp_path, monkeypatch
):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    original_private = root / "original.txt"
    original_private.write_text("original", encoding="utf-8")
    moved_original = tmp_path / "moved-original"
    real_replace = os.replace

    def replace_after_swap(source, destination):
        if Path(source) == root:
            real_replace(root, moved_original)
            root.mkdir()
            (root / "replacement.txt").write_text("replacement", encoding="utf-8")
        return real_replace(source, destination)

    monkeypatch.setattr(data_roots.os, "replace", replace_after_swap)

    with pytest.raises(DataRootValidationError):
        remove_owned_data_root(marker.reference)

    assert (moved_original / "original.txt").read_text(encoding="utf-8") == "original"
    assert (root / "replacement.txt").read_text(encoding="utf-8") == "replacement"
    assert not list(tmp_path.glob(".eve-income-tracker-removing-*"))


def test_remove_owned_root_rejects_replacement_even_with_copied_marker(
    tmp_path, monkeypatch
):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    (root / "original.txt").write_text("original", encoding="utf-8")
    moved_original = tmp_path / "moved-original"
    real_replace = os.replace

    def replace_after_swap(source, destination):
        if Path(source) == root:
            real_replace(root, moved_original)
            root.mkdir()
            (root / ".eve-income-tracker-data.json").write_bytes(
                (moved_original / ".eve-income-tracker-data.json").read_bytes()
            )
            (root / "replacement.txt").write_text("replacement", encoding="utf-8")
        return real_replace(source, destination)

    monkeypatch.setattr(data_roots.os, "replace", replace_after_swap)

    with pytest.raises(DataRootValidationError) as caught:
        remove_owned_data_root(marker.reference)

    assert caught.value.code == "remove_identity_changed"
    assert (moved_original / "original.txt").read_text(encoding="utf-8") == "original"
    assert (root / "replacement.txt").read_text(encoding="utf-8") == "replacement"
    assert not list(tmp_path.glob(".eve-income-tracker-removing-*"))


def test_remove_owned_root_preserves_original_when_quarantine_move_fails(
    tmp_path, monkeypatch
):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    private = root / "keep.txt"
    private.write_text("keep", encoding="utf-8")

    def fail_move(source, destination):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(data_roots.os, "replace", fail_move)

    with pytest.raises(DataRootValidationError) as caught:
        remove_owned_data_root(marker.reference)

    assert caught.value.code == "remove_failed"
    assert private.read_text(encoding="utf-8") == "keep"


def test_remove_owned_root_keeps_quarantine_when_restore_path_is_occupied(
    tmp_path, monkeypatch
):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    moved_original = tmp_path / "moved-original"
    real_replace = os.replace

    def replace_and_occupy(source, destination):
        if Path(source) == root:
            real_replace(root, moved_original)
            root.mkdir()
            (root / "replacement.txt").write_text("replacement", encoding="utf-8")
            real_replace(root, destination)
            root.mkdir()
            (root / "new-owner.txt").write_text("new owner", encoding="utf-8")
            return None
        return real_replace(source, destination)

    monkeypatch.setattr(data_roots.os, "replace", replace_and_occupy)

    with pytest.raises(DataRootValidationError) as caught:
        remove_owned_data_root(marker.reference)

    assert caught.value.code == "remove_restore_failed"
    assert (root / "new-owner.txt").read_text(encoding="utf-8") == "new owner"
    quarantines = list(tmp_path.glob(".eve-income-tracker-removing-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "replacement.txt").read_text(
        encoding="utf-8"
    ) == "replacement"
    assert (moved_original / ".eve-income-tracker-data.json").exists()


def test_remove_owned_root_keeps_quarantine_when_delete_fails(tmp_path, monkeypatch):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    (root / "keep.txt").write_text("keep", encoding="utf-8")

    def fail_delete(path, identity):
        raise OSError("simulated removal failure")

    monkeypatch.setattr(
        data_roots, "_delete_quarantined_tree", fail_delete, raising=False
    )

    with pytest.raises(DataRootValidationError) as caught:
        remove_owned_data_root(marker.reference)

    assert caught.value.code == "remove_failed"
    assert not root.exists()
    quarantines = list(tmp_path.glob(".eve-income-tracker-removing-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_owned_root_rejects_swap_at_final_deletion_boundary(
    tmp_path, monkeypatch
):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    (root / "original.txt").write_text("original", encoding="utf-8")
    moved_original = tmp_path / "moved-original"
    real_scan = data_roots._reject_reparse_tree

    def swap_after_final_scan(path):
        real_scan(path)
        if path.name.startswith(".eve-income-tracker-removing-"):
            os.replace(path, moved_original)
            path.mkdir()
            (path / "replacement.txt").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(data_roots, "_reject_reparse_tree", swap_after_final_scan)

    with pytest.raises(DataRootValidationError):
        remove_owned_data_root(marker.reference)

    assert (moved_original / "original.txt").read_text(encoding="utf-8") == "original"
    replacement_files = list(tmp_path.rglob("replacement.txt"))
    assert len(replacement_files) == 1
    assert replacement_files[0].read_text(encoding="utf-8") == "replacement"


def test_new_root_rejects_parent_traversal_and_nonempty_target(tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    root = chosen / "EVE Income Tracker"
    root.mkdir()
    private = root / "foreign-file.txt"
    private.write_text("keep", encoding="utf-8")
    source = tmp_path / "source" / "EVE Income Tracker"
    install = tmp_path / "program"

    for parent in (chosen / ".." / "chosen", chosen):
        with pytest.raises(DataRootValidationError):
            validate_new_data_root(parent, source_root=source, install_root=install)
    assert private.read_text(encoding="utf-8") == "keep"


def test_new_root_rejects_symlink_parent(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(DataRootValidationError):
        validate_new_data_root(
            link,
            source_root=tmp_path / "source" / "EVE Income Tracker",
            install_root=tmp_path / "program",
        )


def test_remove_owned_root_requires_exact_id_and_preserves_files_on_mismatch(tmp_path):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    private = root / "data" / "eve_income.sqlite3"
    private.parent.mkdir()
    private.write_bytes(b"private database")

    with pytest.raises(DataRootValidationError):
        remove_owned_data_root(DataRootReference(root, uuid4().hex, 1))

    assert private.read_bytes() == b"private database"
    remove_owned_data_root(marker.reference)
    assert not root.exists()
    assert tmp_path.exists()


def test_remove_owned_root_rejects_nested_symlink_and_preserves_all_files(tmp_path):
    root = tmp_path / "EVE Income Tracker"
    root.mkdir()
    marker = marker_for(root)
    write_data_root_marker(root, marker)
    outside = tmp_path / "outside"
    outside.mkdir()
    private = outside / "keep.txt"
    private.write_text("keep", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(DataRootValidationError):
        remove_owned_data_root(marker.reference)

    assert private.read_text(encoding="utf-8") == "keep"
    assert root.exists()
