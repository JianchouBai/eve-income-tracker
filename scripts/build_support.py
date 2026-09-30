from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from app.paths import PROJECT_ROOT, ConfigurationError, ReleaseConfig
from app.version import APP_VERSION

GENERATED_ROOT = PROJECT_ROOT / "build" / "generated"
CALLBACK_URL = "http://localhost:8000/auth/callback"
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{20,128}")
_CONTACT_EMAIL = re.compile(
    r"\b[A-Za-z0-9!#$%&*+/=?^_\x60{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&*+/=?^_\x60{|}~-]+)*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\b"
)
_PLACEHOLDERS = ("replace", "example", "your-email", "client-id", "changeme")


class BuildInputError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(frozen=True)
class PackagingManifest:
    datas: tuple[tuple[Path, str], ...]
    package_data: tuple[str, ...]
    webview_data: tuple[str, ...]
    hidden_imports: tuple[str, ...]
    excludes: tuple[str, ...]
    release_config: Path
    icon: Path
    version_info: Path


def validate_release_config(path: Path) -> ReleaseConfig:
    try:
        config = ReleaseConfig.from_json(Path(path))
        lowered_id = config.eve_client_id.lower()
        lowered_agent = config.eve_user_agent.lower()
        if (
            _CLIENT_ID.fullmatch(config.eve_client_id) is None
            or any(marker in lowered_id for marker in _PLACEHOLDERS)
            or config.eve_callback_url != CALLBACK_URL
            or not 1 <= len(config.eve_user_agent) <= 255
            or "eve-income-tracker" not in lowered_agent
            or _CONTACT_EMAIL.search(config.eve_user_agent) is None
            or any(marker in lowered_agent for marker in _PLACEHOLDERS)
            or any(character in config.eve_user_agent for character in "\r\n")
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", config.esi_compatibility_date) is None
        ):
            raise ValueError
        date.fromisoformat(config.esi_compatibility_date)
        return config
    except (
        ConfigurationError,
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
    ):
        raise BuildInputError("release_config_invalid") from None


def _atomic_write(path: Path, data: bytes, error_code: str) -> Path:
    temporary: Path | None = None
    descriptor = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        return path
    except OSError:
        raise BuildInputError(error_code) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def write_release_config(
    source: Path,
    generated_dir: Path = GENERATED_ROOT,
) -> Path:
    config = validate_release_config(source)
    serialized = (
        json.dumps(
            asdict(config),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return _atomic_write(
        Path(generated_dir) / "release-config.json",
        serialized,
        "release_config_write_failed",
    )


def write_windows_version_info(
    generated_dir: Path = GENERATED_ROOT,
) -> Path:
    try:
        parts = tuple(int(part) for part in APP_VERSION.split("."))
        if len(parts) != 3 or any(part < 0 or part > 65535 for part in parts):
            raise ValueError
    except ValueError:
        raise BuildInputError("version_invalid") from None
    version_tuple = (*parts, 0)
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'EVE Income Tracker'),
          StringStruct('FileDescription', 'EVE Income Tracker'),
          StringStruct('FileVersion', '{APP_VERSION}'),
          StringStruct('InternalName', 'EVE Income Tracker'),
          StringStruct('OriginalFilename', 'EVE Income Tracker.exe'),
          StringStruct('ProductName', 'EVE Income Tracker'),
          StringStruct('ProductVersion', '{APP_VERSION}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    return _atomic_write(
        Path(generated_dir) / "version-info.txt",
        content.encode("utf-8"),
        "version_write_failed",
    )


def packaging_manifest(
    *,
    project_root: Path = PROJECT_ROOT,
    generated_dir: Path = GENERATED_ROOT,
) -> PackagingManifest:
    project_root = Path(project_root)
    generated_dir = Path(generated_dir)
    release_config = generated_dir / "release-config.json"
    return PackagingManifest(
        datas=(
            (project_root / "app" / "templates", "app/templates"),
            (project_root / "app" / "static", "app/static"),
            (release_config, "."),
        ),
        package_data=("tzdata",),
        webview_data=("lib", "js"),
        hidden_imports=(
            "tzdata",
            "webview",
            "webview.platforms.winforms",
            "webview.platforms.edgechromium",
            "webview.platforms.webview2core",
            "clr",
            "pythonnet",
        ),
        excludes=(
            "PyQt5",
            "PyQt6",
            "PySide2",
            "PySide6",
            "cefpython3",
            "webview.platforms.cef",
            "webview.platforms.qt",
            "webview.platforms.gtk",
            "webview.platforms.cocoa",
            "webview.platforms.android",
            "webview.platforms.mshtml",
            "webview.platforms.winui3",
        ),
        release_config=release_config,
        icon=generated_dir / "app-icon.ico",
        version_info=generated_dir / "version-info.txt",
    )
