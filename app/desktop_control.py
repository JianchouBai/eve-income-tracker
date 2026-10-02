"""Thread-safe restart request shared by the desktop loop and web bridge."""

from collections.abc import Callable
from threading import Lock


class DesktopControl:
    def __init__(self, on_restart: Callable[[], None] | None = None) -> None:
        self._lock = Lock()
        self._requested = False
        self._on_restart = on_restart

    def request_restart(self) -> None:
        with self._lock:
            if self._requested:
                return
            self._requested = True
        if self._on_restart is not None:
            self._on_restart()

    def consume_restart(self) -> bool:
        with self._lock:
            requested = self._requested
            self._requested = False
            return requested
