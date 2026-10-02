from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.data_location_store import MemoryDataLocationStore
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

    def reset_authorization(self):
        self.events.append("reset_authorization")
        return "authorization-reset"

    def rollback_authorization_reset(self, reset):
        self.events.append(("rollback_authorization", reset))


class FakeKeyStore:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure

    def load_or_create(self):
        self.events.append("key")
        if self.failure in {"key", "key_replace"}:
            raise KeyStoreError("key_corrupt")
        return Fernet.generate_key()

    def replace_corrupt(self, backup_dir):
        self.events.append(("replace_key", Path(backup_dir)))
        if self.failure == "key_replace":
            raise KeyStoreError("key_unwritable")
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


class FakeWindowEvent:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def fire(self):
        for callback in self.callbacks:
            callback()


class FakeWebView:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure
        self.settings = {}

    def create_window(self, title, url, **kwargs):
        self.events.append(("window", title, url, kwargs))
        if self.failure == "window":
            raise RuntimeError("private-window-detail")
        self.window = SimpleNamespace(
            destroy=lambda: self.events.append("window_close"),
            events=SimpleNamespace(closed=FakeWindowEvent(), loaded=FakeWindowEvent()),
        )
        return self.window

    def start(self, **kwargs):
        self.events.append(("gui", kwargs))
        if self.failure == "gui":
            raise RuntimeError("private-gui-detail")
        self.window.events.loaded.fire()


def fake_paths(tmp_path):
    root = tmp_path / APP_DATA_DIRECTORY
    resources = tmp_path / "resources"
    return AppPaths(
        resource_root=resources,
        data_root=root,
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

    def bridge_factory(*, data_location, desktop_control, directory_picker):
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
        data_location_factory=RecordingLocation,
        data_location_store_factory=MemoryDataLocationStore,
        startup_interaction_factory=lambda: object(),
        recovery_factory=lambda: RecordingRecovery(events),
        progress_factory=lambda: RecordingProgress(events),
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
        "resolve_location",
        "sweep",
        "pending_work",
        "log",
        "webview",
        "ask",
        "database",
        "lifecycle",
        "protector",
        "key_store",
        "key",
        "prepare",
        "complete_initialization",
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
        "success",
        "window_close",
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


def test_corrupt_key_recovery_requires_confirmation_and_preserves_application_data(
    tmp_path,
):
    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        failure="key",
        ask=True,
    )
    paths.database_path.parent.mkdir(parents=True)
    paths.database_path.write_bytes(b"existing-database")

    result = DesktopApplication(
        local_app_data=tmp_path,
        dependencies=dependencies,
    ).run()

    assert result == 0
    recovery_question = next(
        event[1]
        for event in events
        if isinstance(event, tuple) and event[0] == "ask" and "重置本机授权" in event[1]
    )
    assert "角色、分组和钱包历史" in recovery_question
    names = [event[0] if isinstance(event, tuple) else event for event in events]
    assert names.index("key") < names.index("ask")
    assert names.index("ask") < names.index("prepare")
    assert names.index("prepare") < names.index("reset_authorization")
    assert names.index("reset_authorization") < names.index("replace_key")
    assert ("replace_key", paths.backups_dir) in events
    assert not any(isinstance(event, tuple) and event[0] == "error" for event in events)


def test_declining_corrupt_key_recovery_exits_without_resetting_data(tmp_path):
    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        failure="key",
        ask=False,
    )
    paths.database_path.parent.mkdir(parents=True)
    original = b"existing-database"
    paths.database_path.write_bytes(original)

    result = DesktopApplication(
        local_app_data=tmp_path,
        dependencies=dependencies,
    ).run()

    assert result == 1
    assert paths.database_path.read_bytes() == original
    assert "reset_authorization" not in events
    assert not any(
        isinstance(event, tuple) and event[0] == "replace_key" for event in events
    )
    questions = [
        event[1] for event in events if isinstance(event, tuple) and event[0] == "ask"
    ]
    assert len(questions) == 1
    assert "重置本机授权" in questions[0]


def test_failed_corrupt_key_replacement_rolls_back_authorization_reset(tmp_path):
    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        failure="key_replace",
        ask=True,
    )
    paths.database_path.parent.mkdir(parents=True)
    paths.database_path.write_bytes(b"existing-database")

    result = DesktopApplication(
        local_app_data=tmp_path,
        dependencies=dependencies,
    ).run()

    assert result == 1
    assert (
        "rollback_authorization",
        "authorization-reset",
    ) in events
    names = [event[0] if isinstance(event, tuple) else event for event in events]
    assert names.index("reset_authorization") < names.index("replace_key")
    assert names.index("replace_key") < names.index("rollback_authorization")
    assert "server_start" not in events


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


class RecordingRecovery:
    def __init__(self, events):
        self.events = events

    def sweep_stale_temps(self, paths):
        self.events.append(("sweep", paths.data_root))

    def record_success_and_prune(self, paths):
        self.events.append(("success", paths.data_root))

    def record_failed_start(self, paths):
        self.events.append(("failed_start", paths.data_root))


class RecordingLocation:
    def __init__(
        self,
        store,
        *,
        resource_root,
        default_parent,
        existing_validator,
        recovery_service,
    ):
        self.store = store
        self.resource_root = resource_root
        self.default_parent = default_parent
        self.recovery = recovery_service
        self.events = recovery_service.events
        self.root = default_parent / APP_DATA_DIRECTORY

    def resolve_startup(self, legacy_root, interaction):
        from app.data_roots import DataRootReference

        self.events.append("resolve_location")
        return DataRootReference(self.root, "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa", 1)

    def complete_initialization(self, reference):
        self.events.append("complete_initialization")

    def execute_pending_migration(self):
        self.events.append("pending_work")


class RecordingProgress:
    def __init__(self, events):
        self.events = events

    def show(self, stage):
        self.events.append(("progress", stage))

    def close(self):
        self.events.append("progress_close")


def event_names(events):
    return [event[0] if isinstance(event, tuple) else event for event in events]


def test_restart_stays_inside_mutex_and_rebuilds_with_new_root(tmp_path):
    from dataclasses import replace

    from app.data_roots import DataRootReference

    events = []
    dependencies, paths, _, webview = recording_dependencies(tmp_path, events)
    controller = RecordingLocation(
        None,
        resource_root=paths.resource_root,
        default_parent=tmp_path,
        existing_validator=None,
        recovery_service=RecordingRecovery(events),
    )
    target = tmp_path / "other" / APP_DATA_DIRECTORY
    count = 0

    def migrate():
        nonlocal count
        events.append("pending_work")
        count += 1
        if count == 2:
            controller.root = target
            return SimpleNamespace(
                reference=DataRootReference(
                    target, "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb", 1
                )
            )

    controller.execute_pending_migration = migrate
    dependencies = replace(
        dependencies, data_location_factory=lambda *a, **k: controller
    )
    original_builder = dependencies.app_builder

    def build(settings, runtime):
        webview.runtime = runtime
        return original_builder(settings, runtime)

    dependencies = replace(dependencies, app_builder=build)
    original_start = webview.start

    def start(*args, **kwargs):
        original_start(*args, **kwargs)
        if count == 1:
            webview.runtime.desktop_control.request_restart()
            webview.runtime.desktop_control.request_restart()

    webview.start = start
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )
    names = event_names(events)
    assert names.count("mutex") == names.count("mutex_release") == 1
    assert names.count("window") == names.count("server_start") == 2
    assert names.count("success") == 1
    assert names.count("window_close") == 2
    first_close = names.index("window_close")
    second_resolve = names.index(
        "resolve_location", names.index("resolve_location") + 1
    )
    assert names[first_close:second_resolve] == [
        "window_close",
        "server_stop",
        "log_close",
    ]
    assert [e[1] for e in events if isinstance(e, tuple) and e[0] == "database"] == [
        paths.database_path,
        target / "data/eve_income.sqlite3",
    ]


@pytest.mark.parametrize(
    "stage", ["resolve", "migration", "key", "gui", "server_stop", "log_close"]
)
def test_failure_resets_recovery_only_after_root_resolved(tmp_path, stage):
    from dataclasses import replace

    from app.data_location_controller import DataLocationError

    events = []
    dependencies, _paths, _, _ = recording_dependencies(
        tmp_path,
        events,
        failure=stage if stage in {"key", "gui"} else None,
    )
    if stage in {"resolve", "migration"}:
        original = dependencies.data_location_factory

        def factory(*args, **kwargs):
            controller = original(*args, **kwargs)

            def fail():
                raise DataLocationError("private-location-detail")

            if stage == "resolve":
                controller.resolve_startup = lambda *a: fail()
            else:
                controller.execute_pending_migration = fail
            return controller

        dependencies = replace(dependencies, data_location_factory=factory)
    elif stage == "server_stop":
        original = dependencies.server_factory

        def server(*args):
            result = original(*args)
            result.stop = lambda **kwargs: False
            return result

        dependencies = replace(dependencies, server_factory=server)
    elif stage == "log_close":

        def fail_close():
            raise OSError("private-close-detail")

        dependencies = replace(dependencies, logging_closer=fail_close)

    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    names = event_names(events)
    assert names.count("failed_start") == (0 if stage == "resolve" else 1)
    if stage in {"resolve", "migration"}:
        assert "database" not in names
        assert "webview" not in names
    messages = [e[1] for e in events if isinstance(e, tuple) and e[0] == "error"]
    assert len(messages) == 1
    assert "private-" not in messages[0]


def test_restart_failure_is_fixed_chinese_error_and_does_not_open_third_session(
    tmp_path,
):
    from dataclasses import replace

    from app.data_location_controller import DataLocationError

    events = []
    dependencies, _, _, webview = recording_dependencies(tmp_path, events)
    original_factory = dependencies.data_location_factory
    original_builder = dependencies.app_builder

    def factory(*args, **kwargs):
        controller = original_factory(*args, **kwargs)
        original_resolve = controller.resolve_startup
        calls = 0

        def resolve(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DataLocationError("private-restart-detail")
            return original_resolve(*args)

        controller.resolve_startup = resolve
        return controller

    def build(settings, runtime):
        webview.runtime = runtime
        return original_builder(settings, runtime)

    original_start = webview.start

    def start(*args, **kwargs):
        original_start(*args, **kwargs)
        webview.runtime.desktop_control.request_restart()

    webview.start = start
    dependencies = replace(
        dependencies, data_location_factory=factory, app_builder=build
    )
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    assert event_names(events).count("window") == 1
    assert events[-1] == ("error", "数据位置重启失败。请保留现有数据并重新启动应用。")


def test_control_coalesces_concurrent_requests_and_consumes_once():
    from concurrent.futures import ThreadPoolExecutor

    from app.desktop_control import DesktopControl

    closed = []
    control = DesktopControl(lambda: closed.append(True))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: control.request_restart(), range(100)))
    assert closed == [True]
    assert control.consume_restart() is True
    assert control.consume_restart() is False


@pytest.mark.parametrize("invalid", ["missing", "marker", "database", "key"])
def test_configured_invalid_root_never_falls_back_or_constructs_database(
    tmp_path, invalid
):
    from dataclasses import replace

    from app.data_location_controller import DataLocationController
    from app.data_location_store import MemoryDataLocationStore
    from app.data_roots import DataRootReference

    events = []
    dependencies, paths, _, _ = recording_dependencies(tmp_path, events)
    store = MemoryDataLocationStore()
    root = tmp_path / "offline" / APP_DATA_DIRECTORY
    reference = DataRootReference(root, "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa", 1)
    store.begin_current_switch(reference)
    store.finalize_current_switch()
    if invalid != "missing":
        root.mkdir(parents=True)
    if invalid in {"database", "key"}:
        from app.data_roots import DataRootMarker, write_data_root_marker

        write_data_root_marker(
            root, DataRootMarker(reference, "2026-10-01T00:00:00Z", "ready")
        )
    if invalid == "key":
        Database(root / "data/eve_income.sqlite3").initialize()
    dependencies = replace(
        dependencies,
        data_location_factory=DataLocationController,
        data_location_store_factory=lambda: store,
        existing_validator=lambda paths: None,
        startup_interaction_factory=lambda: SimpleNamespace(
            ask_retry_locate_exit=lambda code: "exit"
        ),
    )
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    assert not paths.database_path.exists()
    assert "database" not in event_names(events)
    assert "log" not in event_names(events)
    assert "failed_start" not in event_names(events)


def test_smoke_never_constructs_registry_store_or_native_ui(tmp_path, monkeypatch):
    import builtins
    from dataclasses import replace

    from app.data_location_controller import DataLocationController
    from app.data_location_store import MemoryDataLocationStore
    from app.key_store import FernetKeyStore
    from app.migration_recovery import RecoverySnapshotService

    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path, events, real_database=True
    )
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "winreg":
            raise AssertionError("smoke imported registry")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    captured = []

    def controller(store, **kwargs):
        captured.append(store)
        return DataLocationController(store, **kwargs)

    def forbidden():
        raise AssertionError("smoke called real-user dependency")

    dependencies = replace(
        dependencies,
        data_location_factory=controller,
        data_location_store_factory=forbidden,
        startup_interaction_factory=forbidden,
        progress_factory=forbidden,
        webview_checker=forbidden,
        legacy_picker=forbidden,
        recovery_factory=RecoverySnapshotService,
        protector_factory=lambda: SimpleNamespace(
            protect=lambda v: v, unprotect=lambda v: v
        ),
        key_store_factory=FernetKeyStore,
        smoke_verifier=lambda *args: None,
    )
    assert (
        DesktopApplication(
            local_app_data=tmp_path, smoke_test=True, dependencies=dependencies
        ).run()
        == 0
    )
    assert len(captured) == 1 and isinstance(captured[0], MemoryDataLocationStore)
    assert paths.database_path.is_file()
    assert "success" not in event_names(events)


def test_real_pending_migration_progress_and_recovery_count_once_across_restart(
    tmp_path,
):
    from dataclasses import replace
    from datetime import UTC, datetime

    from app.data_location_controller import DataLocationController
    from app.data_location_store import PendingMigration
    from app.data_roots import DataRootMarker, DataRootReference, write_data_root_marker
    from app.key_store import FernetKeyStore
    from app.migration_recovery import RecoverySnapshotService
    from app.paths import resolve_app_paths

    events = []
    dependencies, paths, _, webview = recording_dependencies(
        tmp_path, events, real_database=True
    )
    store = MemoryDataLocationStore()
    Database(paths.database_path).initialize()
    paths.key_path.parent.mkdir(parents=True)
    paths.key_path.write_bytes(Fernet.generate_key())
    marker = DataRootMarker(
        DataRootReference(paths.data_root, "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa", 1),
        "2026-10-01T00:00:00Z",
        "ready",
    )
    write_data_root_marker(paths.data_root, marker)
    store.begin_current_switch(marker.reference)
    store.finalize_current_switch()
    parent = tmp_path / "new-drive"
    parent.mkdir()
    store.write_pending_migration(PendingMigration(parent, True))
    paths.backups_dir.mkdir()
    stale = paths.backups_dir / "recovery-db-abcdefgh.tmp"
    stale.write_bytes(b"abandoned")
    ordinary = paths.backups_dir / "user-backup.sqlite3"
    ordinary.write_bytes(b"keep")
    recovery = RecoverySnapshotService(clock=lambda: datetime(2026, 10, 2, tzinfo=UTC))
    identity = SimpleNamespace(
        unprotect=lambda value: value, protect=lambda value: value
    )
    original_builder = dependencies.app_builder
    starts = 0

    def build(settings, runtime):
        webview.runtime = runtime
        return original_builder(settings, runtime)

    original_start = webview.start

    def start(*args, **kwargs):
        nonlocal starts
        original_start(*args, **kwargs)
        starts += 1
        if starts == 1:
            webview.runtime.desktop_control.request_restart()

    webview.start = start
    dependencies = replace(
        dependencies,
        data_location_factory=DataLocationController,
        data_location_store_factory=lambda: store,
        recovery_factory=lambda: recovery,
        protector_factory=lambda: identity,
        key_store_factory=FernetKeyStore,
        app_builder=build,
    )
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )
    target = resolve_app_paths(
        frozen=True,
        bundle_root=paths.resource_root,
        data_root=parent / APP_DATA_DIRECTORY,
    )
    assert not paths.data_root.exists()
    assert target.database_path.is_file()
    assert not (target.backups_dir / stale.name).exists()
    assert (target.backups_dir / ordinary.name).read_bytes() == b"keep"
    assert recovery.inspect(target).successful_starts == 1
    names = event_names(events)
    progress_indices = [
        i
        for i, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "progress"
    ]
    assert [events[i][1] for i in progress_indices] == ["preparing", "complete"]
    assert progress_indices[-1] < names.index("progress_close") < names.index("log")
    assert names.count("mutex") == names.count("mutex_release") == 1
    assert names.count("server_start") == 2
    assert names.index("progress_close") < names.index("database")


@pytest.mark.parametrize("failure", ["key", "gui"])
def test_failed_real_start_resets_existing_snapshot_counter(tmp_path, failure):
    from dataclasses import replace

    from app.migration_recovery import RecoverySnapshotService

    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path, events, real_database=True, failure=failure
    )
    Database(paths.database_path).initialize()
    paths.key_path.parent.mkdir(parents=True)
    paths.key_path.write_bytes(b"protected-key")
    recovery = RecoverySnapshotService()
    recovery.create(paths, paths)
    recovery.record_success_and_prune(paths)
    recovery.record_success_and_prune(paths)
    stale = paths.backups_dir / "recovery-update-abcdefgh.tmp"
    stale.write_bytes(b"abandoned")
    dependencies = replace(
        dependencies,
        recovery_factory=lambda: recovery,
        data_location_factory=lambda store, **kwargs: RecordingLocation(
            store, **{**kwargs, "recovery_service": RecordingRecovery(events)}
        ),
    )
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    assert recovery.inspect(paths).successful_starts == 0
    assert not stale.exists()


@pytest.mark.parametrize("invalid", ["missing", "corrupt", "future", "schema", "key"])
def test_readonly_existing_validator_never_creates_or_repairs_files(tmp_path, invalid):
    import sqlite3

    from app.desktop import DesktopStartupError, _validate_existing_data

    paths = fake_paths(tmp_path)
    if invalid != "missing":
        Database(paths.database_path).initialize()
        paths.key_path.parent.mkdir(parents=True)
        paths.key_path.write_bytes(Fernet.generate_key())
    if invalid == "corrupt":
        paths.database_path.write_bytes(b"corrupt")
    if invalid == "future":
        with sqlite3.connect(paths.database_path) as db:
            db.execute("PRAGMA user_version = 999")
    if invalid == "schema":
        with sqlite3.connect(paths.database_path) as db:
            db.execute("DROP TABLE characters")
    if invalid == "key":
        paths.key_path.write_bytes(b"not-a-key")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(DesktopStartupError):
        _validate_existing_data(paths, lambda: SimpleNamespace(unprotect=lambda v: v))
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_native_closed_window_is_not_destroyed_twice(tmp_path):
    events = []
    dependencies, _, _, webview = recording_dependencies(tmp_path, events)

    closed = FakeWindowEvent()
    loaded = FakeWindowEvent()

    def destroy():
        raise RuntimeError("window already closed")

    webview.create_window = lambda *a, **k: SimpleNamespace(
        destroy=destroy, events=SimpleNamespace(closed=closed, loaded=loaded)
    )

    def start(**kwargs):
        loaded.fire()
        closed.fire()

    webview.start = start
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )


def test_import_prompt_explains_optional_choice_and_picker_precedes_database(tmp_path):
    events = []
    dependencies, _, _, _ = recording_dependencies(tmp_path, events, ask=True)
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )
    prompt = next(e[1] for e in events if isinstance(e, tuple) and e[0] == "ask")
    assert prompt.splitlines()[0] == "导入旧版数据库（可选）"
    assert "跳过并新建数据库" in prompt
    names = event_names(events)
    assert (
        names.index("resolve_location")
        < names.index("choose_database")
        < names.index("database")
    )


def test_loaded_event_records_success_only_after_window_ready_and_only_once(tmp_path):
    events = []
    dependencies, _, _, webview = recording_dependencies(tmp_path, events)

    def start(started=None, **kwargs):
        # pywebview starts func before creating its native window.
        if started is not None:
            started()
        assert "success" not in event_names(events)
        webview.window.events.loaded.fire()
        webview.window.events.loaded.fire()
        assert event_names(events).count("success") == 1

    webview.start = start
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )


def test_native_close_can_dispatch_closed_event_on_another_thread(tmp_path):
    from threading import Thread

    events = []
    dependencies, _, _, webview = recording_dependencies(tmp_path, events)
    original_create = webview.create_window

    def create(*args, **kwargs):
        window = original_create(*args, **kwargs)

        def destroy():
            thread = Thread(target=window.events.closed.fire, daemon=True)
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive(), "GUI callback blocked behind destroy lock"

        window.destroy = destroy
        return window

    webview.create_window = create
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )


def test_delayed_callback_from_closed_session_does_not_count_restarted_session(
    tmp_path,
):
    from dataclasses import replace

    events = []
    dependencies, _, _, webview = recording_dependencies(tmp_path, events)
    old_loaded = None
    runs = 0
    original_builder = dependencies.app_builder

    def build(settings, runtime):
        webview.runtime = runtime
        return original_builder(settings, runtime)

    def start(**kwargs):
        nonlocal old_loaded, runs
        runs += 1
        if runs == 1:
            old_loaded = webview.window.events.loaded
            webview.runtime.desktop_control.request_restart()
        else:
            old_loaded.fire()
            assert "success" not in event_names(events)
            webview.window.events.loaded.fire()

    webview.start = start
    dependencies = replace(dependencies, app_builder=build)
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )
    assert event_names(events).count("success") == 1


def test_unknown_runtime_error_code_is_sanitized_in_log_and_dialog(tmp_path, caplog):
    from dataclasses import replace

    events = []
    dependencies, _, _, _ = recording_dependencies(tmp_path, events)

    def fail(server):
        raise LocalServerError("private-token-from-runtime")

    dependencies = replace(dependencies, health_waiter=fail)
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    assert "private-token-from-runtime" not in caplog.text
    assert "private-token-from-runtime" not in repr(events)
    assert "error_code=startup_failed" in caplog.text


def test_progress_closes_on_migration_failure_before_database_or_logging(tmp_path):
    from dataclasses import replace

    from app.data_location_store import PendingMigration

    events = []
    dependencies, _, _, _ = recording_dependencies(tmp_path, events)
    store = MemoryDataLocationStore()
    store.write_pending_migration(PendingMigration(tmp_path / "target", True))
    original = dependencies.data_location_factory

    def factory(*args, **kwargs):
        location = original(*args, **kwargs)

        def fail():
            raise OSError("private-migration-failure")

        location.execute_pending_migration = fail
        return location

    dependencies = replace(
        dependencies,
        data_location_store_factory=lambda: store,
        data_location_factory=factory,
    )
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    names = event_names(events)
    assert (
        names.index("progress")
        < names.index("progress_close")
        < names.index("mutex_release")
    )
    assert "database" not in names
    assert "log" not in names
    assert names.count("failed_start") == 1


def test_loaded_callback_failure_never_escapes_to_native_event_logger(tmp_path):
    from dataclasses import replace

    events = []
    dependencies, _, _, webview = recording_dependencies(tmp_path, events)
    recovery = RecordingRecovery(events)

    def failed_record(paths):
        raise RuntimeError("private-recovery-error")

    recovery.record_success_and_prune = failed_record
    original = webview.create_window

    def create(*args, **kwargs):
        window = original(*args, **kwargs)

        def fail_destroy():
            raise RuntimeError("private-window-error")

        window.destroy = fail_destroy
        return window

    escaped = []

    def start(**kwargs):
        try:
            webview.window.events.loaded.fire()
        except RuntimeError as error:
            escaped.append(str(error))

    webview.create_window = create
    webview.start = start
    dependencies = replace(dependencies, recovery_factory=lambda: recovery)
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 1
    )
    assert escaped == []


@pytest.mark.parametrize("failed_stage", ["webview", "prepare", "marker"])
def test_fresh_failed_launch_resumes_exact_root_on_next_process(
    tmp_path, monkeypatch, failed_stage
):
    from dataclasses import replace

    from app.data_location_controller import DataLocationController
    from app.data_roots import read_data_root_marker
    from app.key_store import FernetKeyStore

    events = []
    dependencies, paths, _, _ = recording_dependencies(
        tmp_path, events, real_database=True
    )
    store = MemoryDataLocationStore()
    identity = SimpleNamespace(protect=lambda v: v, unprotect=lambda v: v)
    interaction = SimpleNamespace(
        choose_initial_location=lambda root: "default",
        confirm_location=lambda *args: True,
        ask_retry_locate_exit=lambda code: "exit",
    )
    dependencies = replace(
        dependencies,
        data_location_factory=DataLocationController,
        data_location_store_factory=lambda: store,
        startup_interaction_factory=lambda: interaction,
        protector_factory=lambda: identity,
        key_store_factory=FernetKeyStore,
    )
    if failed_stage == "webview":
        first = replace(dependencies, webview_checker=lambda: None)
    elif failed_stage == "prepare":

        class FailedPrepare:
            def prepare(self):
                raise DatabaseLifecycleError("database_unwritable")

        first = replace(dependencies, lifecycle_factory=lambda *args: FailedPrepare())

    else:
        import app.data_location_controller as controller_module

        original_write = controller_module.write_data_root_marker

        def fail_ready(root, marker):
            if marker.state == "ready":
                raise OSError("private-marker-write-failure")
            original_write(root, marker)

        monkeypatch.setattr(controller_module, "write_data_root_marker", fail_ready)
        first = dependencies

    assert DesktopApplication(local_app_data=tmp_path, dependencies=first).run() == 1
    if failed_stage == "marker":
        monkeypatch.setattr(controller_module, "write_data_root_marker", original_write)
    reference = store.read_current()
    assert reference.root == paths.data_root
    assert read_data_root_marker(reference.root).state == "incomplete"
    assert paths.database_path.exists() is (failed_stage == "marker")
    if failed_stage in {"prepare", "marker"}:
        assert paths.key_path.is_file()

    # A new controller/process uses the explicit stored continuation, no wizard.
    def forbidden(*args):
        raise AssertionError("must not select a second root")

    interaction.choose_initial_location = forbidden
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )
    assert store.read_current() == reference
    assert read_data_root_marker(reference.root).state == "ready"
    assert Database(paths.database_path).inspect_version() == LATEST_SCHEMA_VERSION


def test_desktop_injects_only_storage_capabilities_into_window_bridge(tmp_path):
    events = []
    dependencies, _, _, _ = recording_dependencies(tmp_path, events)
    selected_parent = tmp_path / "chosen"
    from dataclasses import replace

    dependencies = replace(
        dependencies,
        startup_interaction_factory=lambda: SimpleNamespace(
            choose_data_parent=lambda: selected_parent
        ),
    )
    constructed = []

    def factory(*, data_location, desktop_control, directory_picker):
        constructed.append((data_location, desktop_control, directory_picker()))
        return object()

    dependencies = replace(dependencies, bridge_factory=factory)
    assert (
        DesktopApplication(local_app_data=tmp_path, dependencies=dependencies).run()
        == 0
    )
    runtime = next(
        item[2] for item in events if isinstance(item, tuple) and item[0] == "app"
    )
    assert constructed == [
        (runtime.data_location, runtime.desktop_control, selected_parent)
    ]
