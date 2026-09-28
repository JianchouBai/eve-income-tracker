import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = ("127.0.0.1", 8765)
BASE_URL = "http://127.0.0.1:8765"


def check_home(client: httpx.Client) -> None:
    response = client.get(BASE_URL + "/")
    if (
        response.status_code != 200
        or "text/html" not in response.headers.get("content-type", "")
        or "<html" not in response.text.lower()
    ):
        raise RuntimeError("Home HTML check failed.")


def wait_for_health(process: subprocess.Popen, client: httpx.Client) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Uvicorn exited before becoming healthy.")
        try:
            response = client.get(BASE_URL + "/healthz")
        except httpx.TransportError:
            time.sleep(0.1)
            continue
        try:
            healthy = response.status_code == 200 and response.json() == {
                "status": "ok"
            }
        except ValueError:
            healthy = False
        if not healthy:
            raise RuntimeError("Health JSON check failed.")
        return
    raise RuntimeError("Uvicorn startup deadline exceeded.")


def stop_child(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_smoke() -> None:
    # Never mistake an existing server for this smoke run or stop its owner.
    with socket.socket() as reservation:
        if os.name == "nt":
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            reservation.bind(ADDRESS)
        except OSError:
            raise RuntimeError(
                "127.0.0.1:8765 is unavailable; no process was stopped."
            ) from None

    with tempfile.TemporaryDirectory(prefix="eve-income-smoke-") as directory:
        environment = {
            **{
                name: value
                for name, value in os.environ.items()
                if name not in {"UVICORN_RELOAD", "UVICORN_WORKERS", "WEB_CONCURRENCY"}
            },
            # Override every operational field, including values from any real .env.
            "EVE_CLIENT_ID": "",
            "EVE_CLIENT_SECRET": "",
            "EVE_CALLBACK_URL": "http://localhost:8000/auth/callback",
            "APP_SESSION_SECRET": "",
            "APP_ENCRYPTION_KEY": "",
            "DATABASE_PATH": str(Path(directory) / "smoke.sqlite3"),
            "EVE_USER_AGENT": "eve-income-tracker/0.1 (smoke@example.invalid)",
            "ESI_COMPATIBILITY_DATE": "2026-05-19",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # Missing credentials deliberately prevent building any SSO/ESI services.
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                str(ROOT),
                "--host",
                ADDRESS[0],
                "--port",
                str(ADDRESS[1]),
                "--workers",
                "1",
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            # Fixed loopback URLs, disabled proxies and redirects: no external network.
            with httpx.Client(
                timeout=0.5, trust_env=False, follow_redirects=False
            ) as client:
                wait_for_health(process, client)
                check_home(client)
                if process.poll() is not None:
                    raise RuntimeError("Uvicorn exited during smoke verification.")
        finally:
            stop_child(process)
    print("SMOKE_OK healthz=200 home=200")


if __name__ == "__main__":
    try:
        run_smoke()
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
    ) as error:
        print(f"SMOKE_FAILED {error}", file=sys.stderr)
        raise SystemExit(1) from None
