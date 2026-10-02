from __future__ import annotations

import os
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from app.data_location_controller import DataLocationController
from app.desktop_control import DesktopControl


class AuthorizationUrlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class DesktopStorageError(RuntimeError):
    """Fixed bridge errors; never include native exception details."""


def _open_directory(path: Path) -> None:
    os.startfile(path)


class DesktopBridge:
    def __init__(
        self,
        *,
        opener: Callable[[str], bool] = webbrowser.open_new_tab,
        data_location: DataLocationController | None = None,
        desktop_control: DesktopControl | None = None,
        directory_picker: Callable[[], Path | None] | None = None,
        directory_opener: Callable[[Path], object] = _open_directory,
    ) -> None:
        self._opener = opener
        self._data_location = data_location
        self._desktop_control = desktop_control
        self._directory_picker = directory_picker
        self._directory_opener = directory_opener

    def choose_data_parent(self) -> str | None:
        try:
            if self._directory_picker is None:
                raise DesktopStorageError("storage_unavailable")
            parent = self._directory_picker()
            return None if parent is None else str(parent)
        except Exception:  # noqa: BLE001 - native boundary must not disclose details.
            raise DesktopStorageError("directory_picker_failed") from None

    def open_data_directory(self) -> bool:
        try:
            if self._data_location is None:
                raise DesktopStorageError("storage_unavailable")
            current = self._data_location.status().current_path
            if self._directory_opener(current) is False:
                raise DesktopStorageError("directory_open_failed")
            return True
        except Exception:  # noqa: BLE001 - never expose shell details.
            raise DesktopStorageError("directory_open_failed") from None

    def restart_for_migration(self) -> bool:
        try:
            if self._data_location is None or self._desktop_control is None:
                raise DesktopStorageError("storage_unavailable")
            pending = self._data_location.status().pending_migration
        except Exception:  # noqa: BLE001 - fixed public failure.
            raise DesktopStorageError("migration_restart_failed") from None
        if pending is None:
            raise DesktopStorageError("migration_not_pending")
        try:
            self._desktop_control.request_restart()
        except Exception:  # noqa: BLE001 - never expose native shutdown details.
            raise DesktopStorageError("migration_restart_failed") from None
        return True

    def open_authorization_url(self, url: str) -> bool:
        if not self._is_allowed(url):
            raise AuthorizationUrlError("authorization_url_rejected")
        try:
            opened = self._opener(url)
        except Exception:  # noqa: BLE001 - expose no shell or browser details.
            raise AuthorizationUrlError("browser_open_failed") from None
        if opened is not True:
            raise AuthorizationUrlError("browser_open_failed")
        return True

    @staticmethod
    def _is_allowed(url: str) -> bool:
        if (
            not isinstance(url, str)
            or not url
            or any(character.isspace() for character in url)
        ):
            return False
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == "login.eveonline.com"
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )


class DesktopBridgeFactory(Protocol):
    def __call__(
        self,
        *,
        data_location: DataLocationController,
        desktop_control: DesktopControl,
        directory_picker: Callable[[], Path | None],
    ) -> DesktopBridge: ...
