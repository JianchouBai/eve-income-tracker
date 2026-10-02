from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.desktop import SMOKE_RESULT_NAME, SMOKE_SUCCESS
from app.paths import APP_DATA_DIRECTORY

_SMOKE_PREFIX = "eve-income-frozen-smoke-"
_REMOVED_ENVIRONMENT = {
    "EVE_CLIENT_ID",
    "EVE_CLIENT_SECRET",
    "EVE_CALLBACK_URL",
    "EVE_USER_AGENT",
    "ESI_COMPATIBILITY_DATE",
    "APP_SESSION_SECRET",
    "APP_ENCRYPTION_KEY",
    "DATABASE_PATH",
    "RUNTIME_MODE",
    "UVICORN_RELOAD",
    "UVICORN_WORKERS",
    "WEB_CONCURRENCY",
    "PYWEBVIEW_GUI",
    "PYTHONPATH",
    "VIRTUAL_ENV",
}


class FrozenSmokeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


def _isolated_environment(owned_root: Path) -> dict[str, str]:
    environment = {}
    for name, value in os.environ.items():
        normalized = name.upper()
        if normalized in _REMOVED_ENVIRONMENT or normalized.endswith("_PROXY"):
            continue
        environment[name] = value
    environment["LOCALAPPDATA"] = str(owned_root)
    for name, child in {
        "APPDATA": "roaming",
        "USERPROFILE": "profile",
        "HOME": "profile",
        "TEMP": "temp",
        "TMP": "temp",
    }.items():
        directory = owned_root / child
        directory.mkdir(exist_ok=True)
        environment[name] = str(directory)
    environment["HOMEDRIVE"] = owned_root.drive
    environment["HOMEPATH"] = str(owned_root / "profile")[len(owned_root.drive) :]
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_frozen_smoke(
    executable: Path,
    temp_parent: Path,
    *,
    timeout: float = 45,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    executable = Path(executable).resolve()
    temp_parent = Path(temp_parent).resolve()
    if not executable.is_file() or timeout <= 0:
        raise FrozenSmokeError("smoke_input_invalid")
    try:
        temp_parent.mkdir(parents=True, exist_ok=True)
        owned_root = Path(
            tempfile.mkdtemp(
                prefix=_SMOKE_PREFIX,
                dir=temp_parent,
            )
        ).resolve()
    except OSError:
        raise FrozenSmokeError("smoke_temp_failed") from None
    if owned_root.parent != temp_parent or not owned_root.name.startswith(
        _SMOKE_PREFIX
    ):
        raise FrozenSmokeError("smoke_temp_failed")

    command = [
        str(executable),
        "--smoke-test",
        "--local-app-data",
        str(owned_root),
    ]
    marker = owned_root / APP_DATA_DIRECTORY / "config" / SMOKE_RESULT_NAME
    try:
        try:
            completed = runner(
                command,
                cwd=executable.parent,
                env=_isolated_environment(owned_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except subprocess.TimeoutExpired:
            raise FrozenSmokeError("smoke_timeout") from None
        except (OSError, subprocess.SubprocessError):
            raise FrozenSmokeError("smoke_process_failed") from None
        if completed.returncode != 0:
            raise FrozenSmokeError("smoke_process_failed")
        try:
            marker_bytes = marker.read_bytes()
        except OSError:
            raise FrozenSmokeError("smoke_contract_failed") from None
        if marker_bytes != (SMOKE_SUCCESS + "\n").encode("utf-8"):
            raise FrozenSmokeError("smoke_contract_failed")
    finally:
        try:
            if owned_root.parent != temp_parent or not owned_root.name.startswith(
                _SMOKE_PREFIX
            ):
                raise FrozenSmokeError("smoke_cleanup_failed")
            shutil.rmtree(owned_root)
        except FrozenSmokeError:
            raise
        except OSError:
            raise FrozenSmokeError("smoke_cleanup_failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test frozen desktop executable")
    parser.add_argument("executable", type=Path)
    parser.add_argument(
        "--temp-parent",
        type=Path,
        default=Path(tempfile.gettempdir()),
    )
    parser.add_argument("--timeout", type=float, default=45)
    arguments = parser.parse_args(argv)
    try:
        run_frozen_smoke(
            arguments.executable,
            arguments.temp_parent,
            timeout=arguments.timeout,
        )
    except FrozenSmokeError as error:
        if sys.stderr is not None:
            print(f"FROZEN_SMOKE_FAILED code={error.code}", file=sys.stderr)
        return 1
    if sys.stdout is not None:
        print("FROZEN_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
