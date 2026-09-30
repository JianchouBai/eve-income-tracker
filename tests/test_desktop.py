from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.database_lifecycle import DatabaseLifecycle, DatabaseLifecycleError
from app.desktop import (
    DESKTOP_LOG_NAME,
    DesktopApplication,
    DesktopDependencies,
    main,
)
from app.desktop_server import LocalServerError
from app.key_store import KeyStoreError
from app.paths import (
    APP_DATA_DIRECTORY,
    AppPaths,
    ConfigurationError,
    ReleaseConfig,
)
from app.storage import LATEST_SCHEMA_VERSION, Database


class FakeGuard:
    def __init__(self, events, acquired=True):
        self.events = events
        self.acquired = acquired

    def acquire(self):
        self.events.append("mutex")
        return self.acquired

    def release(self):
        self.events.append("mutex_release")


class FakeLifecycle:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure

    def import_legacy(self, source):
        self.events.append(("import", Path(source)))
        if self.failure == "import":
            raise DatabaseLifecycleError("import_invalid")

    def prepare(self):
        self.events.append("prepare")
        failures = {
            "prepare_future": "database_future",
            "prepare_corrupt": "database_corrupt",
            "prepare_migration": "migration_failed",
        }
        if self.failure in failures:
            raise DatabaseLifecycleError(failures[self.failure])


class FakeKeyStore:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure

    def load_or_create(self):
        self.events.append("key")
        if self.failure == "key":
            raise KeyStoreError("key_corrupt")
        return Fernet.generate_key()


class FakeLaunchTokens:
    def __init__(self, events):
        self.events = events

    def issue(self):
        self.events.append("launch_token")
        return "desktop-launch-token"


class FakeServer:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure
        self.port = 8000

    def start(self):
        self.events.append("server_start")
        if self.failure == "server_start":
            raise LocalServerError("port_unavailable")
        return self.port

    def stop(self, timeout=2):
        self.events.append("server_stop")
        return True


class FakeWebView:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure
        self.settings = {}

    def create_window(self, title, url, **kwargs):
        self.events.append(("window", title, url, kwargs))
        if self.failure == "window":
            raise RuntimeError("private-window-detail")
        return object()

    def start(self, **kwargs):
        self.events.append(("gui", kwargs))
        if self.failure == "gui":
            raise RuntimeError("private-gui-detail")


def fake_paths(tmp_path):
    root = tmp_path / APP_DATA_DIRECTORY
    resources = tmp_path / "resources"
    return AppPaths(
        resource_root=resources,
        data_dir=root / "data",
        config_dir=root / "config",
        logs_dir=root / "logs",
        backups_dir=root / "backups",
        database_path=root / "data" / "eve_income.sqlite3",
        key_path=root / "config" / "local.key",
        webview_storage_dir=root / "config" / "webview",
    )


def valid_release_config():
    return ReleaseConfig(
        eve_client_id="public-client-id",
        eve_callback_url="http://localhost:8000/auth/callback",
        eve_user_agent="eve-income-tracker/0.1 (admin@example.com)",
        esi_compatibility_date="2026-05-19",
    )


def recording_dependencies(
    tmp_path,
    events,
    *,
    failure=None,
    acquired=True,
    webview_available=True,
    ask=False,
    selected=None,
    real_database=False,
):
    paths = fake_paths(tmp_path)
    lifecycle_holder = {}
    fake_webview = FakeWebView(events, failure)

    def resolve_paths(local_app_data):
        events.append(("paths", local_app_data))
        return paths

    def configure_logging(path):
        events.append(("log", path))
        if failure == "log":
            raise OSError("private-log-detail")

    def close_logging():
        events.append("log_close")

    def check_webview():
        events.append("webview")
        return "123.0.0.0" if webview_available else None

    def ask_question(message):
        events.append(("ask", message))
        return ask

    def choose_database():
        events.append("choose_database")
        return selected

    def database_factory(path):
        events.append(("database", path))
        return Database(path) if real_database else SimpleNamespace(path=path)

    def lifecycle_factory(database, backups_dir):
        events.append(("lifecycle", backups_dir))
        lifecycle = (
            DatabaseLifecycle(database, backups_dir)
            if real_database
            else FakeLifecycle(events, failure)
        )
        lifecycle_holder["value"] = lifecycle
        return lifecycle

    def protector_factory():
        events.append("protector")
        return object()

    def key_store_factory(path, protector):
        events.append(("key_store", path, protector))
        return FakeKeyStore(events, failure)

    def load_release(path):
        events.append(("config", path))
        if failure == "config":
            raise ConfigurationError("private-config-detail")
        return valid_release_config()

    runtime = SimpleNamespace(
        mode="desktop",
        launch_tokens=FakeLaunchTokens(events),
        attempts=object(),
    )

    def runtime_factory():
        events.append("runtime")
        return runtime

    def app_builder(settings, supplied_runtime):
        events.append(("app", settings, supplied_runtime))
        return object()

    fake_server = FakeServer(events, failure)

    def server_factory(app):
        events.append(("server", app))
        return fake_server

    def health_waiter(server):
        events.append(("health", server))
        if failure == "health":
            raise LocalServerError("health_timeout")

    def bridge_factory():
        events.append("bridge")
        return object()

    def load_webview():
        events.append("webview_loader")
        return fake_webview

    def show_error(message):
        events.append(("error", message))

    dependencies = DesktopDependencies(
        mutex_factory=lambda: FakeGuard(events, acquired),
        path_resolver=resolve_paths,
        logging_configurer=configure_logging,
        logging_closer=close_logging,
        webview_checker=check_webview,
        question_asker=ask_question,
        legacy_picker=choose_database,
        database_factory=database_factory,
        lifecycle_factory=lifecycle_factory,
        protector_factory=protector_factory,
        key_store_factory=key_store_factory,
        release_config_loader=load_release,
        runtime_factory=runtime_factory,
        app_builder=app_builder,
        server_factory=server_factory,
        health_waiter=health_waiter,
        bridge_factory=bridge_factory,
        webview_loader=load_webview,
        error_shower=show_error,
        session_secret_factory=lambda: "s" * 64,
    )
    return dependencies, paths, lifecycle_holder, fake_webview


def test_ordered_desktop_bootstrap_and_owned_cleanup(tmp_path):
    events = []
    dependencies, paths, _, webview = recording_dependencies(tmp_path, events)

    result = DesktopApplication(
        local_app_data=tmp_path,
        dependencies=dependencies,
    ).run()

    assert result == 0
    names = [item[0] if isinstance(item, tuple) else item for item in events]
    assert names == [
        "mutex",
        "paths",
        "log",
        "webview",
        "database",
        "lifecycle",
        "ask",
        "protector",
        "key_store",
        "key",
        "prepare",
        "config",
        "runtime",
        "app",
        "server",
        "server_start",
        "health",
        "launch_token",
        "webview_loader",
        "bridge",
        "window",
        "gui",
        "server_stop",
        "log_close",
        "mutex_release",
    ]
    window_event = next(
        item for item in events if isinstance(item, tuple) and item[0] == "window"
    )
    assert window_event[1] == "EVE 收益追踪器"
    assert window_event[2] == "http://localhost:8000/?launch_token=desktop-launch-token"
    assert window_event[3]["width"] == 1180
    assert window_event[3]["height"] == 780
    assert window_event[3]["resizable"] is True
    assert window_event[3]["js_api"] is not None
    gui_event = next(
        item for item in events if isinstance(item, tuple) and item[0] == "gui"
    )
    assert gui_event[1] == {
        "gui": "edgechromium",
        "debug": False,
        "private_mode": True,
        "storage_path": str(paths.webview_storage_dir),
    }
    assert webview.settings["ALLOW_DOWNLOADS"] is False
    assert webview.settings["ALLOW_FILE_URLS"] is False
    assert webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] is False
    assert webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is False
    assert webview.settings["REMOTE_DEBUGGING_PORT"] is None


@pytest.mark.parametrize(
    ("ask", "selected", "expects_import"),
    [
        (False, None, False),
        (True, None, False),
        (True, "source", True),
    ],
)
def test_first_run_import_choice_happens_before_new_database_prepare(
    tmp_path,
    ask,
    selected,
    expects_import,
):
    events = []
    selected_path = tmp_path / "legacy.sqlite3" if selected else None
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        ask=ask,
        selected=selected_path,
    )

    assert (
        DesktopApplication(
            local_app_data=tmp_path,
            dependencies=dependencies,
        ).run()
        == 0
    )

    imports = [
        event for event in events if isinstance(event, tuple) and event[0] == "import"
    ]
    assert bool(imports) is expects_import
    if expects_import:
        assert events.index(imports[0]) < events.index("prepare")
    assert not paths.database_path.exists()


@pytest.mark.parametrize(
    ("ask", "selected"),
    [
        (False, None),
        (True, None),
    ],
)
def test_declining_or_cancelling_import_creates_new_database(tmp_path, ask, selected):
    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        ask=ask,
        selected=selected,
        real_database=True,
    )

    assert (
        DesktopApplication(
            local_app_data=tmp_path,
            dependencies=dependencies,
        ).run()
        == 0
    )

    assert paths.database_path.is_file()
    assert Database(paths.database_path).inspect_version() == LATEST_SCHEMA_VERSION
    assert not any(
        isinstance(event, tuple) and event[0] == "import" for event in events
    )


def test_existing_database_skips_first_run_prompt(tmp_path):
    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        real_database=True,
    )
    Database(paths.database_path).initialize()

    assert (
        DesktopApplication(
            local_app_data=tmp_path,
            dependencies=dependencies,
        ).run()
        == 0
    )

    assert not any(isinstance(event, tuple) and event[0] == "ask" for event in events)
    assert "choose_database" not in events


def test_real_first_run_import_precedes_prepare_and_preserves_source(tmp_path):
    events = []
    source = tmp_path / "legacy.sqlite3"
    Database(source).initialize()
    source_before = source.read_bytes()
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        ask=True,
        selected=source,
        real_database=True,
    )

    assert (
        DesktopApplication(
            local_app_data=tmp_path,
            dependencies=dependencies,
        ).run()
        == 0
    )

    assert source.read_bytes() == source_before
    assert paths.database_path.exists()
    assert Database(paths.database_path).inspect_version() == LATEST_SCHEMA_VERSION


def test_failed_real_import_changes_neither_source_nor_target(tmp_path):
    events = []
    source = tmp_path / "broken.sqlite3"
    source.write_bytes(b"not a sqlite database")
    source_before = source.read_bytes()
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        ask=True,
        selected=source,
        real_database=True,
    )

    result = DesktopApplication(
        local_app_data=tmp_path,
        dependencies=dependencies,
    ).run()

    assert result == 1
    assert source.read_bytes() == source_before
    assert not paths.database_path.exists()
    assert "protector" not in events
    error = next(
        event[1] for event in events if isinstance(event, tuple) and event[0] == "error"
    )
    assert "导入" in error


@pytest.mark.parametrize(
    ("failure", "acquired", "webview_available", "message_fragment"),
    [
        (None, False, True, "已经运行"),
        ("log", True, True, "数据目录"),
        (None, True, False, "WebView2"),
        ("key", True, True, "本机密钥"),
        ("prepare_future", True, True, "更高版本"),
        ("prepare_corrupt", True, True, "损坏"),
        ("prepare_migration", True, True, "升级失败"),
        ("config", True, True, "发布配置"),
        ("server_start", True, True, "8000"),
        ("health", True, True, "本地服务"),
        ("window", True, True, "桌面窗口"),
        ("gui", True, True, "桌面窗口"),
    ],
)
def test_startup_failure_matrix_is_actionable_and_releases_only_owned_resources(
    tmp_path,
    failure,
    acquired,
    webview_available,
    message_fragment,
):
    events = []
    dependencies, _, _, _ = recording_dependencies(
        tmp_path,
        events,
        failure=failure,
        acquired=acquired,
        webview_available=webview_available,
    )

    result = DesktopApplication(
        local_app_data=tmp_path,
        dependencies=dependencies,
    ).run()

    assert result == 1
    messages = [
        event[1] for event in events if isinstance(event, tuple) and event[0] == "error"
    ]
    assert len(messages) == 1
    assert message_fragment in messages[0]
    assert all("private-" not in message for message in messages)
    assert events[-1] == ("error", messages[0])
    assert "mutex_release" in events
    if any(isinstance(event, tuple) and event[0] == "server" for event in events):
        assert "server_stop" in events
    if (
        any(isinstance(event, tuple) and event[0] == "log" for event in events)
        and failure != "log"
    ):
        assert "log_close" in events
    if not acquired:
        assert not any(
            isinstance(event, tuple) and event[0] == "log" for event in events
        )


def test_desktop_settings_are_explicit_and_ignore_environment_values(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "private-env.sqlite3"))
    monkeypatch.setenv("APP_SESSION_SECRET", "private-env-session-" * 3)
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    events = []
    dependencies, paths, _, _ = recording_dependencies(tmp_path, events)

    assert (
        DesktopApplication(
            local_app_data=tmp_path,
            dependencies=dependencies,
        ).run()
        == 0
    )

    app_event = next(
        event for event in events if isinstance(event, tuple) and event[0] == "app"
    )
    settings = app_event[1]
    assert isinstance(settings, Settings)
    assert settings.runtime_mode == "desktop"
    assert settings.database_path == paths.database_path
    assert settings.resource_root == paths.resource_root
    assert settings.app_session_secret == "s" * 64
    assert "private-env" not in repr(settings.model_dump())


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="desktop entry targets Windows"
)
def test_source_smoke_uses_only_temporary_local_app_data(tmp_path, capsys):
    result = main(["--smoke-test", "--local-app-data", str(tmp_path)])

    assert result == 0
    assert (
        "DESKTOP_SMOKE_OK healthz=200 home=200 resources=ok database=ok"
        in capsys.readouterr().out
    )
    private_root = tmp_path / APP_DATA_DIRECTORY
    assert (private_root / "data" / "eve_income.sqlite3").is_file()
    assert (private_root / "config" / "local.key").is_file()
    assert (private_root / "logs" / DESKTOP_LOG_NAME).is_file()


def test_windows_requirements_pin_only_pywebview_backend_dependency():
    requirements = (
        Path("requirements-windows.txt").read_text(encoding="utf-8").splitlines()
    )

    assert requirements == ["-r requirements.txt", "pywebview==6.2.1"]
    assert not any(
        "qt" in line.lower() or "cef" in line.lower() for line in requirements
    )
