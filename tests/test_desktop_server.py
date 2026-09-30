import socket
import threading
import time

import pytest
from fastapi import FastAPI

from app.desktop_server import (
    LOOPBACK_HOST,
    LocalServer,
    LocalServerError,
    wait_until_healthy,
)


def healthy_app():
    app = FastAPI()

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    return app


def test_real_loopback_server_becomes_healthy_and_releases_its_port():
    server = LocalServer(healthy_app(), port=0)
    try:
        assert server.start() > 0
        wait_until_healthy(server, timeout=3)
        port = server.port
        assert server.host == LOOPBACK_HOST
        assert server.config.access_log is False
        assert server.config.workers == 1
    finally:
        assert server.stop(timeout=3) is True

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((LOOPBACK_HOST, port))
    finally:
        probe.close()


def test_occupied_port_is_reported_without_closing_or_killing_owner():
    owner = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    owner.bind((LOOPBACK_HOST, 0))
    owner.listen()
    port = owner.getsockname()[1]
    server = LocalServer(healthy_app(), port=port)

    try:
        with pytest.raises(LocalServerError) as caught:
            server.start()
        assert caught.value.code == "port_unavailable"
        assert owner.fileno() >= 0
        assert owner.getsockname()[1] == port
    finally:
        server.stop()
        owner.close()


@pytest.mark.skipif(not socket.has_ipv6, reason="IPv6 loopback is unavailable")
def test_real_server_owns_ipv6_loopback_on_the_same_port():
    server = LocalServer(healthy_app(), port=0)
    try:
        port = server.start()
        wait_until_healthy(server, timeout=3)
        with socket.create_connection(("::1", port), timeout=1):
            pass
    finally:
        assert server.stop(timeout=3) is True


@pytest.mark.skipif(not socket.has_ipv6, reason="IPv6 loopback is unavailable")
def test_ipv6_occupied_port_is_reported_and_ipv4_listener_is_released():
    owner = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    owner.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    owner.bind(("::1", 0))
    owner.listen()
    port = owner.getsockname()[1]
    server = LocalServer(healthy_app(), port=port)

    try:
        with pytest.raises(LocalServerError) as caught:
            server.start()
        assert caught.value.code == "port_unavailable"
        assert owner.fileno() >= 0

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((LOOPBACK_HOST, port))
        finally:
            probe.close()
    finally:
        server.stop()
        owner.close()


def test_wait_detects_server_thread_that_exits_before_health():
    class ExitingServer:
        should_exit = False
        force_exit = False

        def __init__(self, config):
            self.config = config

        def run(self, sockets):
            return None

    server = LocalServer(
        healthy_app(),
        port=0,
        server_factory=ExitingServer,
    )
    server.start()

    with pytest.raises(LocalServerError) as caught:
        wait_until_healthy(server, timeout=1)

    assert caught.value.code == "server_exited"
    assert server.stop() is True


def test_wait_requires_exact_health_payload_and_times_out_boundedly():
    app = FastAPI()

    @app.get("/healthz")
    async def wrong_health():
        return {"status": "starting"}

    server = LocalServer(app, port=0)
    server.start()
    started = time.monotonic()
    try:
        with pytest.raises(LocalServerError) as caught:
            wait_until_healthy(server, timeout=0.2, interval=0.02)
    finally:
        server.stop(timeout=3)

    assert caught.value.code == "health_timeout"
    assert time.monotonic() - started < 2


def test_stop_is_bounded_when_server_ignores_shutdown_without_terminating_process():
    release = threading.Event()

    class StubbornServer:
        should_exit = False
        force_exit = False

        def __init__(self, config):
            self.config = config

        def run(self, sockets):
            release.wait(2)

    server = LocalServer(
        healthy_app(),
        port=0,
        server_factory=StubbornServer,
    )
    server.start()
    started = time.monotonic()

    assert server.stop(timeout=0.02) is False
    assert time.monotonic() - started < 0.5

    release.set()
    assert server.stop(timeout=1) is True
