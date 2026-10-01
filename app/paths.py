from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DATA_DIRECTORY = "EVE Income Tracker"
_RELEASE_FIELDS = frozenset(
    {
        "eve_client_id",
        "eve_callback_url",
        "eve_user_agent",
        "esi_compatibility_date",
    }
)


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AppPaths:
    resource_root: Path
    data_root: Path
    data_dir: Path
    config_dir: Path
    logs_dir: Path
    backups_dir: Path
    database_path: Path
    key_path: Path
    webview_storage_dir: Path


@dataclass(frozen=True)
class ReleaseConfig:
    eve_client_id: str
    eve_callback_url: str
    eve_user_agent: str
    esi_compatibility_date: str

    @classmethod
    def from_json(cls, path: Path) -> ReleaseConfig:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != _RELEASE_FIELDS:
                raise ValueError
            values = {}
            for field in _RELEASE_FIELDS:
                value = payload[field]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError
                values[field] = value.strip()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            raise ConfigurationError("release configuration is invalid") from None
        return cls(**values)


def resolve_app_paths(
    *,
    frozen: bool,
    local_app_data: Path | None = None,
    bundle_root: Path | None = None,
    data_root: Path | None = None,
) -> AppPaths:
    if frozen:
        if bundle_root is None:
            frozen_root = getattr(sys, "_MEIPASS", None)
            bundle_root = (
                Path(frozen_root)
                if frozen_root is not None
                else Path(sys.executable).resolve().parent
            )
        if data_root is None:
            if local_app_data is None:
                raw_local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
                if not raw_local_app_data:
                    raise ConfigurationError("LOCALAPPDATA is unavailable")
                local_app_data = Path(raw_local_app_data)
            private_root = local_app_data / APP_DATA_DIRECTORY
        else:
            private_root = data_root.resolve()
    else:
        bundle_root = bundle_root or PROJECT_ROOT
        private_root = bundle_root

    data_dir = private_root / "data"
    config_dir = private_root / "config"
    return AppPaths(
        resource_root=bundle_root,
        data_root=private_root,
        data_dir=data_dir,
        config_dir=config_dir,
        logs_dir=private_root / "logs",
        backups_dir=private_root / "backups",
        database_path=data_dir / "eve_income.sqlite3",
        key_path=config_dir / "local.key",
        webview_storage_dir=config_dir / "webview",
    )
