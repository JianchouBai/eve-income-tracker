from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import uvicorn

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class LocalServerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class LocalServer:
    def __init__(
        self,
        app: Any,
        *,
        host: str = LOOPBACK_HOST,
        port: int = DEFAULT_PORT,
        server_factory: Callable[[uvicorn.Config], Any] = uvicorn.Server,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError("desktop server must bind only to 127.0.0.1")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port is invalid")
        self.host = host
        self.port = port
        self.config = uvicorn.Config(
            app,
            host=host,
            port=port,
            workers=1,
            access_log=False,
            log_level="critical",
            log_config=None,
            timeout_graceful_shutdown=1,
        )
        self._server_factory = server_factory
        self._server: Any | None = None
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._thread_failure = False
        self._lock = threading.Lock()

    def start(self) -> int:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.port
            if self._thread is not None:
                raise LocalServerError("server_cannot_restart")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_EXCLUSIVEADDRUSE,
                        1,
                    )
                listener.bind((self.host, self.port))
                listener.listen(2048)
            except OSError:
                listener.close()
                raise LocalServerError("port_unavailable") from None
            self.port = listener.getsockname()[1]
            self.config.port = self.port
            self._socket = listener
            self._server = self._server_factory(self.config)
            self._thread = threading.Thread(
                target=self._serve,
                name="eve-income-local-server",
                daemon=True,
            )
            self._thread.start()
            return self.port

    def _serve(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        except BaseException:  # noqa: BLE001 - thread failure is surfaced as a safe code.
            self._thread_failure = True
        finally:
            listener, self._socket = self._socket, None
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass

    def wait_until_healthy(
        self,
        *,
        timeout: float = 10,
        interval: float = 0.05,
    ) -> None:
        if timeout <= 0 or interval <= 0:
            raise ValueError("timeout and interval must be positive")
        thread = self._thread
        if thread is None:
            raise LocalServerError("server_not_started")
        deadline = time.monotonic() + timeout
        url = f"http://{self.host}:{self.port}/healthz"
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=min(0.25, timeout),
        ) as client:
            while True:
                if not thread.is_alive():
                    raise LocalServerError("server_exited")
                try:
                    response = client.get(url)
                    if response.status_code == 200 and response.json() == {
                        "status": "ok"
                    }:
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalServerError("health_timeout")
                time.sleep(min(interval, remaining))

    def stop(self, *, timeout: float = 2) -> bool:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        server = self._server
        thread = self._thread
        if thread is None:
            return True
        if server is not None:
            server.should_exit = True
        thread.join(timeout)
        if thread.is_alive():
            if server is not None:
                server.force_exit = True
            listener = self._socket
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            thread.join(min(timeout, 0.1))
        return not thread.is_alive()


def wait_until_healthy(
    server: LocalServer,
    *,
    timeout: float = 10,
    interval: float = 0.05,
) -> None:
    server.wait_until_healthy(timeout=timeout, interval=interval)
