import importlib
import importlib.util
import json
from pathlib import Path

import pytest


def paths_module():
    assert importlib.util.find_spec("app.paths") is not None, "app.paths is missing"
    return importlib.import_module("app.paths")


def write_release_config(path: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "eve_client_id": "public-client-id",
        "eve_callback_url": "http://localhost:8000/auth/callback",
        "eve_user_agent": "eve-income-tracker/0.1 (admin@example.com)",
        "esi_compatibility_date": "2026-05-19",
    }
    values.update(overrides)
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_development_paths_use_explicit_root_without_creating_directories(tmp_path):
    paths = paths_module()
    project = tmp_path / "源代码 工作区"

    resolved = paths.resolve_app_paths(frozen=False, bundle_root=project)

    assert resolved.resource_root == project
    assert resolved.data_dir == project / "data"
    assert resolved.database_path == project / "data" / "eve_income.sqlite3"
    assert resolved.config_dir == project / "config"
    assert resolved.key_path == project / "config" / "local.key"
    assert resolved.logs_dir == project / "logs"
    assert resolved.backups_dir == project / "backups"
    assert resolved.webview_storage_dir == project / "config" / "webview"
    assert not project.exists()


def test_frozen_paths_match_documented_local_app_data_layout(tmp_path):
    paths = paths_module()
    local_app_data = tmp_path / "用户 数据"
    bundle = tmp_path / "只读 程序"

    resolved = paths.resolve_app_paths(
        frozen=True,
        local_app_data=local_app_data,
        bundle_root=bundle,
    )

    root = local_app_data / "EVE Income Tracker"
    assert resolved.resource_root == bundle
    assert resolved.data_dir == root / "data"
    assert resolved.database_path == root / "data" / "eve_income.sqlite3"
    assert resolved.config_dir == root / "config"
    assert resolved.key_path == root / "config" / "local.key"
    assert resolved.logs_dir == root / "logs"
    assert resolved.backups_dir == root / "backups"
    assert resolved.webview_storage_dir == root / "config" / "webview"
    assert not root.exists()


def test_two_windows_users_never_share_private_paths(tmp_path):
    paths = paths_module()
    bundle = tmp_path / "bundle"

    first = paths.resolve_app_paths(
        frozen=True, local_app_data=tmp_path / "user-a", bundle_root=bundle
    )
    second = paths.resolve_app_paths(
        frozen=True, local_app_data=tmp_path / "user-b", bundle_root=bundle
    )

    assert first.resource_root == second.resource_root
    assert first.database_path != second.database_path
    assert first.key_path != second.key_path
    assert first.logs_dir != second.logs_dir
    assert first.backups_dir != second.backups_dir


def test_release_config_accepts_only_four_public_fields(tmp_path):
    paths = paths_module()
    config_path = write_release_config(tmp_path / "release-config.json")

    config = paths.ReleaseConfig.from_json(config_path)

    assert config.eve_client_id == "public-client-id"
    assert config.eve_callback_url == "http://localhost:8000/auth/callback"
    assert config.eve_user_agent == "eve-income-tracker/0.1 (admin@example.com)"
    assert config.esi_compatibility_date == "2026-05-19"


@pytest.mark.parametrize(
    "overrides",
    [
        {"eve_client_id": ""},
        {"eve_callback_url": None},
        {"eve_user_agent": "   "},
        {"esi_compatibility_date": 20260519},
        {"eve_client_secret": "never-echo-private-secret"},
        {"app_encryption_key": "never-echo-private-key"},
        {"database_path": "never-echo-private-path"},
    ],
)
def test_release_config_rejects_invalid_or_private_fields_without_echoing_values(
    tmp_path, overrides
):
    paths = paths_module()
    config_path = write_release_config(tmp_path / "release-config.json", **overrides)

    with pytest.raises(paths.ConfigurationError) as caught:
        paths.ReleaseConfig.from_json(config_path)

    rendered = str(caught.value)
    assert "never-echo" not in rendered
    assert str(config_path) not in rendered
