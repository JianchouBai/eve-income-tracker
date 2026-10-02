from __future__ import annotations

import argparse
import importlib
import logging
import secrets
import sqlite3
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.application import build_app
from app.auth_attempts import AuthorizationAttemptStore
from app.config import Settings
from app.data_location_controller import DataLocationController, DataLocationError
from app.data_location_store import MemoryDataLocationStore, WindowsDataLocationStore
from app.database_lifecycle import (
    DatabaseLifecycle,
    DatabaseLifecycleError,
    _read_only_connection,
    _schema_is_supported,
)
from app.desktop_bridge import DesktopBridge, DesktopBridgeFactory
from app.desktop_control import DesktopControl
from app.desktop_server import LocalServer, LocalServerError, wait_until_healthy
from app.desktop_session import DesktopLaunchTokenStore
from app.key_store import FernetKeyStore, KeyStoreError, WindowsDpapiProtector
from app.logging_setup import close_desktop_logging, configure_desktop_logging
from app.migration_recovery import RecoverySnapshotService
from app.paths import (
    PROJECT_ROOT,
    AppPaths,
    ConfigurationError,
    ReleaseConfig,
    resolve_app_paths,
)
from app.security import validate_fernet_key
from app.storage import LATEST_SCHEMA_VERSION, Database
from app.web import RuntimeContext
from app.windows_runtime import (
    APP_TITLE,
    MigrationProgress,
    NativeStartupInteraction,
    SingleInstanceGuard,
    WindowsRuntimeError,
    ask_yes_no,
    choose_legacy_database,
    show_native_error,
    webview2_version,
)

DESKTOP_LOG_NAME = "eve-income-tracker.log"
CALLBACK_URL = "http://localhost:8000/auth/callback"
FIRST_RUN_QUESTION = (
    "导入旧版数据库（可选）\n"
    "选择“否”或取消即可跳过并新建数据库，为这台电脑创建新的独立数据。"
)
KEY_RECOVERY_QUESTION = (
    "本机授权密钥已损坏或无法由当前 Windows 用户解密。是否重置本机授权？\n\n"
    "选择“是”将先备份数据库和损坏密钥，清除已保存的角色授权令牌；"
    "角色、分组和钱包历史会保留，之后需要逐个重新授权。\n"
    "选择“否”将退出且不修改授权数据。"
)
SMOKE_SUCCESS = "DESKTOP_SMOKE_OK healthz=200 home=200 resources=ok database=ok"
SMOKE_RESULT_NAME = "desktop-smoke-result.txt"

_ERROR_MESSAGES = {
    "location_failed": "无法使用所选数据目录。请确认磁盘已连接，并重试或定位已有数据目录。",
    "restart_failed": "数据位置重启失败。请保留现有数据并重新启动应用。",
    "already_running": "EVE 收益追踪器已经运行，请先切换到现有窗口。",
    "mutex_create_failed": "无法建立单实例保护。请重新启动 Windows 后再试。",
    "webview_missing": (
        "未检测到 Microsoft Edge WebView2 Runtime。请重新运行安装程序以安装 WebView2。"
    ),
    "data_unwritable": (
        "无法写入本机数据目录。请检查当前 Windows 用户对所选数据目录的权限。"
    ),
    "key_failed": (
        "无法读取或创建本机密钥。请确认使用原 Windows 用户运行；"
        "如密钥已损坏，请保留数据库并重新授权角色。"
    ),
    "database_future": (
        "数据库由更高版本的应用创建，当前版本不能安全打开。请安装较新的版本。"
    ),
    "database_locked": "数据库正被其他程序占用。请关闭其他实例后重试。",
    "database_corrupt": ("数据库已损坏或格式不受支持。请保留原文件并从备份恢复。"),
    "database_unwritable": (
        "无法创建本机数据库。请检查 所选数据目录的可用空间与写入权限。"
    ),
    "backup_unwritable": (
        "无法创建数据库安全备份。原数据库未修改，请检查备份目录权限。"
    ),
    "migration_failed": (
        "数据库升级失败，已尝试恢复原文件。请保留日志和备份后再重试。"
    ),
    "database_restore_failed": (
        "数据库操作失败且无法自动恢复。请停止操作并从 backups 目录恢复。"
    ),
    "authorization_reset_failed": (
        "重置角色授权失败，数据库已恢复。请保留日志和备份后重试。"
    ),
    "import_failed": ("旧数据库导入失败，源文件未修改。请确认文件未被占用后重试。"),
    "config_invalid": ("安装包中的公开发布配置无效。请重新安装正式构建的应用。"),
    "port_unavailable": (
        "本地端口 8000 已被其他程序占用。请关闭占用程序后重新启动；应用不会终止该程序。"
    ),
    "server_exited": "本地服务在启动完成前退出。请查看本机日志后重试。",
    "health_timeout": "本地服务未能按时启动。请查看本机日志后重试。",
    "window_failed": ("无法创建桌面窗口。请确认 WebView2 可用，然后重新启动应用。"),
    "shutdown_failed": "本地服务未能正常停止。请等待片刻后再重新启动应用。",
    "startup_failed": "应用启动失败。请查看本机日志后重试。",
}


def _emit(stream: Any, message: str) -> None:
    if stream is None:
        return
    try:
        print(message, file=stream)
    except (AttributeError, OSError):
        pass


class DesktopStartupError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


def _resolve_desktop_paths(local_app_data: Path | None) -> AppPaths:
    frozen = bool(getattr(sys, "frozen", False))
    bundle_root = (
        Path(sys._MEIPASS)
        if frozen and getattr(sys, "_MEIPASS", None)
        else PROJECT_ROOT
    )
    return resolve_app_paths(
        frozen=True,
        local_app_data=local_app_data,
        bundle_root=bundle_root,
    )


def _load_webview() -> Any:
    return importlib.import_module("webview")


def _new_runtime() -> RuntimeContext:
    return RuntimeContext(
        mode="desktop",
        attempts=AuthorizationAttemptStore(),
        launch_tokens=DesktopLaunchTokenStore(),
    )


def _new_session_secret() -> str:
    return secrets.token_urlsafe(48)


def _smoke_release_config() -> ReleaseConfig:
    return ReleaseConfig(
        eve_client_id="desktop-smoke-public-client",
        eve_callback_url=CALLBACK_URL,
        eve_user_agent="eve-income-tracker/0.1 (smoke@example.com)",
        esi_compatibility_date="2026-05-19",
    )


def _verify_smoke(
    paths: AppPaths,
    server: LocalServer,
    launch_token: str,
    database: Database,
) -> None:
    base_url = f"http://localhost:{server.port}"
    with httpx.Client(
        base_url=base_url,
        trust_env=False,
        follow_redirects=True,
        timeout=1,
    ) as client:
        home = client.get("/", params={"launch_token": launch_token})
        css = client.get("/static/app.css")
        script = client.get("/static/app.js")
    if (
        home.status_code != 200
        or "text/html" not in home.headers.get("content-type", "")
        or "EVE" not in home.text
    ):
        raise DesktopStartupError("health_timeout")
    if (
        css.status_code != 200
        or script.status_code != 200
        or "data-desktop-authorization" not in script.text
    ):
        raise DesktopStartupError("health_timeout")
    if database.inspect_version() != LATEST_SCHEMA_VERSION:
        raise DesktopStartupError("database_corrupt")
    if not paths.database_path.is_file():
        raise DesktopStartupError("database_corrupt")


def _new_location_store():
    # Source/test overrides never consult the installed user's pointer.
    if not getattr(sys, "frozen", False):
        return MemoryDataLocationStore()
    return WindowsDataLocationStore()


class _SmokeInteraction:
    def choose_initial_location(self, default_root):
        return "default"

    def confirm_location(self, root, estimate=None):
        return True


def _validate_existing_data(paths: AppPaths, protector_factory) -> None:
    """Read-only checks: never construct Database or create/repair a key."""
    try:
        connection = _read_only_connection(paths.database_path)
        try:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise DesktopStartupError("database_corrupt")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        if version > LATEST_SCHEMA_VERSION:
            raise DesktopStartupError("database_future")
        if not _schema_is_supported(paths.database_path, version):
            raise DesktopStartupError("database_corrupt")
        validate_fernet_key(protector_factory().unprotect(paths.key_path.read_bytes()))
    except (OSError, ValueError, sqlite3.Error, KeyStoreError):
        raise DesktopStartupError("existing_data_invalid") from None


@dataclass(frozen=True, slots=True)
class DesktopDependencies:
    mutex_factory: Callable[[], Any] = SingleInstanceGuard
    path_resolver: Callable[[Path | None], AppPaths] = _resolve_desktop_paths
    data_location_store_factory: Callable[[], Any] = _new_location_store
    data_location_factory: Callable[..., Any] = DataLocationController
    startup_interaction_factory: Callable[[], Any] = NativeStartupInteraction
    recovery_factory: Callable[[], Any] = RecoverySnapshotService
    progress_factory: Callable[[], Any] = MigrationProgress
    existing_validator: Callable[[AppPaths], None] | None = None
    logging_configurer: Callable[[Path], None] = configure_desktop_logging
    logging_closer: Callable[[], None] = close_desktop_logging
    webview_checker: Callable[[], str | None] = webview2_version
    question_asker: Callable[[str], bool] = ask_yes_no
    legacy_picker: Callable[[], Path | None] = choose_legacy_database
    database_factory: Callable[[Path], Any] = Database
    lifecycle_factory: Callable[[Any, Path], Any] = DatabaseLifecycle
    protector_factory: Callable[[], Any] = WindowsDpapiProtector
    key_store_factory: Callable[[Path, Any], Any] = FernetKeyStore
    release_config_loader: Callable[[Path], ReleaseConfig] = ReleaseConfig.from_json
    runtime_factory: Callable[[], Any] = _new_runtime
    app_builder: Callable[[Settings, Any], Any] = build_app
    server_factory: Callable[[Any], Any] = LocalServer
    health_waiter: Callable[[Any], None] = wait_until_healthy
    bridge_factory: DesktopBridgeFactory = DesktopBridge
    webview_loader: Callable[[], Any] = _load_webview
    error_shower: Callable[[str], None] = show_native_error
    session_secret_factory: Callable[[], str] = _new_session_secret
    smoke_verifier: Callable[[AppPaths, Any, str, Any], None] = _verify_smoke


def _error_code(error: BaseException) -> str:
    if isinstance(
        error,
        DesktopStartupError | LocalServerError | WindowsRuntimeError,
    ):
        return error.code if error.code in _ERROR_MESSAGES else "startup_failed"
    if isinstance(error, DataLocationError):
        return "location_failed"
    if isinstance(error, KeyStoreError):
        return "key_failed"
    if isinstance(error, DatabaseLifecycleError):
        if error.code.startswith("import_"):
            return "import_failed"
        return error.code if error.code in _ERROR_MESSAGES else "startup_failed"
    if isinstance(error, ConfigurationError):
        return "config_invalid"
    if isinstance(error, OSError):
        return "data_unwritable"
    return "startup_failed"


def _settings_from_release(
    release: ReleaseConfig,
    paths: AppPaths,
    key: bytes,
    session_secret: str,
) -> Settings:
    if release.eve_callback_url != CALLBACK_URL:
        raise DesktopStartupError("config_invalid")
    try:
        encryption_key = key.decode("ascii")
    except (AttributeError, UnicodeDecodeError):
        raise DesktopStartupError("key_failed") from None
    settings = Settings(
        _env_file=None,
        runtime_mode="desktop",
        resource_root=paths.resource_root,
        eve_client_id=release.eve_client_id,
        eve_callback_url=release.eve_callback_url,
        app_session_secret=session_secret,
        app_encryption_key=encryption_key,
        database_path=paths.database_path,
        eve_user_agent=release.eve_user_agent,
        esi_compatibility_date=release.esi_compatibility_date,
    )
    if settings.configuration_issues():
        raise DesktopStartupError("config_invalid")
    return settings


class DesktopApplication:
    def __init__(
        self,
        *,
        local_app_data: Path | None = None,
        smoke_test: bool = False,
        dependencies: DesktopDependencies | None = None,
    ) -> None:
        self._local_app_data = (
            Path(local_app_data) if local_app_data is not None else None
        )
        self._smoke_test = smoke_test
        self._dependencies = dependencies or DesktopDependencies()

    def run(self) -> int:
        dependencies = self._dependencies
        guard = None
        failure_code = None
        smoke_result_path = None
        successful_start_recorded = False
        restarting = False
        try:
            guard = dependencies.mutex_factory()
            if not guard.acquire():
                raise DesktopStartupError("already_running")
            if self._smoke_test and self._local_app_data is None:
                raise DesktopStartupError("location_failed")
            local_parent = (
                self._local_app_data.resolve()
                if self._local_app_data is not None
                else None
            )
            defaults = dependencies.path_resolver(local_parent)
            store = (
                MemoryDataLocationStore()
                if self._smoke_test
                else dependencies.data_location_store_factory()
            )
            recovery = dependencies.recovery_factory()
            validator = dependencies.existing_validator or (
                lambda paths: _validate_existing_data(
                    paths, dependencies.protector_factory
                )
            )
            controller = dependencies.data_location_factory(
                store,
                resource_root=defaults.resource_root,
                default_parent=defaults.data_root.parent,
                existing_validator=validator,
                recovery_service=recovery,
            )
            interaction = (
                _SmokeInteraction()
                if self._smoke_test
                else dependencies.startup_interaction_factory()
            )

            def run_session():
                nonlocal smoke_result_path
                failure_code = None
                paths = None
                server = None
                window = None
                logging_active = False
                window_closed = False
                window_lock = threading.RLock()
                callback_lock = threading.Lock()
                callback_errors = []
                session_done = False

                def close_window():
                    nonlocal window_closed
                    with window_lock:
                        if window is None or window_closed:
                            return
                        window_closed = True
                    # Native destroy dispatches synchronously to the GUI thread;
                    # never hold its event-state lock during that dispatch.
                    try:
                        window.destroy()
                    except Exception:
                        with window_lock:
                            window_closed = False
                        callback_errors.append("shutdown_failed")
                        raise

                def mark_window_closed():
                    nonlocal window_closed
                    with window_lock:
                        window_closed = True

                control = DesktopControl(close_window)

                def started():
                    nonlocal successful_start_recorded
                    with callback_lock:
                        if session_done:
                            return
                        try:
                            if not successful_start_recorded:
                                recovery.record_success_and_prune(paths)
                                successful_start_recorded = True
                        except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                            callback_errors.append("startup_failed")
                            try:
                                close_window()
                            except Exception:  # noqa: BLE001 - never leak to native event logger.
                                logging.getLogger(__name__).warning(
                                    "Desktop cleanup failed: stage=window",
                                )

                try:
                    reference = controller.resolve_startup(
                        defaults.data_root, interaction
                    )
                    paths = resolve_app_paths(
                        frozen=True,
                        bundle_root=defaults.resource_root,
                        data_root=reference.root,
                    )
                    recovery.sweep_stale_temps(paths)
                    pending = (
                        store.read_pending_migration() or store.read_pending_cleanup()
                    )
                    progress = (
                        dependencies.progress_factory()
                        if pending and not self._smoke_test
                        else None
                    )
                    try:
                        if progress is not None:
                            progress.show("preparing")
                        result = controller.execute_pending_migration()
                        if result is not None:
                            reference = result.reference
                            paths = resolve_app_paths(
                                frozen=True,
                                bundle_root=defaults.resource_root,
                                data_root=result.reference.root,
                            )
                            recovery.sweep_stale_temps(paths)
                        if progress is not None:
                            progress.show("complete")
                    finally:
                        if progress is not None:
                            progress.close()
                    # The selected root is authoritative even when interrupted work
                    # completed its switch without returning a new migration result.
                    current = store.read_current()
                    if current is not None and current.root != paths.data_root:
                        reference = current
                        paths = resolve_app_paths(
                            frozen=True,
                            bundle_root=defaults.resource_root,
                            data_root=current.root,
                        )
                        recovery.sweep_stale_temps(paths)
                    logging_active = True
                    dependencies.logging_configurer(paths.logs_dir / DESKTOP_LOG_NAME)
                    if self._smoke_test:
                        smoke_result_path = paths.config_dir / SMOKE_RESULT_NAME
                    if not self._smoke_test and not dependencies.webview_checker():
                        raise DesktopStartupError("webview_missing")

                    source = None
                    if (
                        not paths.database_path.exists()
                        and not self._smoke_test
                        and dependencies.question_asker(FIRST_RUN_QUESTION)
                    ):
                        source = dependencies.legacy_picker()
                    database = dependencies.database_factory(paths.database_path)
                    lifecycle = dependencies.lifecycle_factory(
                        database, paths.backups_dir
                    )
                    if source is not None:
                        lifecycle.import_legacy(source)
                    protector = dependencies.protector_factory()
                    key_store = dependencies.key_store_factory(
                        paths.key_path,
                        protector,
                    )
                    try:
                        key = key_store.load_or_create()
                    except KeyStoreError as error:
                        if (
                            self._smoke_test
                            or error.code != "key_corrupt"
                            or not dependencies.question_asker(KEY_RECOVERY_QUESTION)
                        ):
                            raise
                        lifecycle.prepare()
                        reset = lifecycle.reset_authorization()
                        try:
                            key = key_store.replace_corrupt(paths.backups_dir)
                        except Exception:
                            lifecycle.rollback_authorization_reset(reset)
                            raise
                    else:
                        lifecycle.prepare()

                    controller.complete_initialization(reference)

                    release = (
                        _smoke_release_config()
                        if self._smoke_test
                        else dependencies.release_config_loader(
                            paths.resource_root / "release-config.json"
                        )
                    )
                    settings = _settings_from_release(
                        release,
                        paths,
                        key,
                        dependencies.session_secret_factory(),
                    )
                    runtime = dependencies.runtime_factory()
                    runtime.data_location = controller
                    runtime.desktop_control = control
                    application = dependencies.app_builder(settings, runtime)
                    server = dependencies.server_factory(application)
                    server.start()
                    dependencies.health_waiter(server)
                    launch_token = runtime.launch_tokens.issue()

                    if self._smoke_test:
                        dependencies.smoke_verifier(
                            paths,
                            server,
                            launch_token,
                            database,
                        )
                    else:
                        paths.webview_storage_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            webview = dependencies.webview_loader()
                            webview.settings.update(
                                {
                                    "ALLOW_DOWNLOADS": False,
                                    "ALLOW_FILE_URLS": False,
                                    "OPEN_DEVTOOLS_IN_DEBUG": False,
                                    "OPEN_EXTERNAL_LINKS_IN_BROWSER": False,
                                    "REMOTE_DEBUGGING_PORT": None,
                                }
                            )
                            authorization_bridge = dependencies.bridge_factory(
                                data_location=controller,
                                desktop_control=control,
                                directory_picker=lambda: (
                                    interaction.choose_data_parent()
                                ),
                            )
                            launch_url = "http://localhost:8000/?" + urlencode(
                                {"launch_token": launch_token}
                            )
                            window = webview.create_window(
                                APP_TITLE,
                                launch_url,
                                js_api=authorization_bridge,
                                width=1180,
                                height=780,
                                min_size=(900, 600),
                                resizable=True,
                                zoomable=False,
                            )
                            window.events.closed += mark_window_closed
                            window.events.loaded += started
                            webview.start(
                                gui="edgechromium",
                                debug=False,
                                private_mode=True,
                                storage_path=str(paths.webview_storage_dir),
                            )
                        except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.  # noqa: BLE001 - expose only fixed desktop error.
                            raise DesktopStartupError("window_failed") from None

                    if callback_errors:
                        raise DesktopStartupError(callback_errors[0])
                except Exception as error:  # noqa: BLE001 - sanitize startup boundary failures.
                    failure_code = (
                        "restart_failed" if restarting else _error_code(error)
                    )
                finally:
                    with callback_lock:
                        session_done = True
                        if callback_errors:
                            failure_code = failure_code or callback_errors[0]
                    try:
                        close_window()
                    except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                        failure_code = failure_code or "shutdown_failed"
                    if server is not None:
                        try:
                            stopped = server.stop(timeout=3)
                        except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                            stopped = False
                        if not stopped:
                            failure_code = failure_code or "shutdown_failed"
                    if failure_code and paths is not None:
                        logging.getLogger(__name__).warning(
                            "Desktop application failed: error_code=%s",
                            failure_code,
                        )
                    if logging_active:
                        try:
                            dependencies.logging_closer()
                        except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                            failure_code = failure_code or "shutdown_failed"
                    if failure_code and paths is not None and not self._smoke_test:
                        try:
                            recovery.record_failed_start(paths)
                        except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                            logging.getLogger(__name__).warning(
                                "Desktop cleanup failed: stage=recovery",
                            )
                return failure_code, control.consume_restart()

            while True:
                failure_code, restart_requested = run_session()
                if failure_code or self._smoke_test or not restart_requested:
                    break
                restarting = True
        except Exception as error:  # noqa: BLE001 - sanitize startup boundary failures.
            failure_code = "restart_failed" if restarting else _error_code(error)
        finally:
            if guard is not None:
                try:
                    guard.release()
                except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                    failure_code = failure_code or "shutdown_failed"

        if failure_code is not None:
            message = _ERROR_MESSAGES.get(
                failure_code, _ERROR_MESSAGES["startup_failed"]
            )
            if self._smoke_test:
                _emit(sys.stderr, f"DESKTOP_SMOKE_FAILED code={failure_code}")
            else:
                try:
                    dependencies.error_shower(message)
                except Exception:  # noqa: BLE001 - sanitize native/runtime boundary failures.
                    logging.getLogger(__name__).warning("Desktop error dialog failed")
            return 1
        if self._smoke_test:
            if smoke_result_path is None:
                _emit(sys.stderr, "DESKTOP_SMOKE_FAILED code=smoke_contract_failed")
                return 1
            try:
                smoke_result_path.parent.mkdir(parents=True, exist_ok=True)
                smoke_result_path.write_bytes((SMOKE_SUCCESS + "\n").encode("utf-8"))
            except OSError:
                _emit(sys.stderr, "DESKTOP_SMOKE_FAILED code=smoke_contract_failed")
                return 1
            _emit(sys.stdout, SMOKE_SUCCESS)
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EVE 收益追踪器 Windows 桌面入口")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="运行不访问 EVE 的本地打包烟雾检查",
    )
    parser.add_argument(
        "--local-app-data",
        type=Path,
        help="覆盖 LocalAppData 根目录（仅用于测试）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.smoke_test and arguments.local_app_data is None:
        with tempfile.TemporaryDirectory(
            prefix="eve-income-desktop-smoke-"
        ) as directory:
            return DesktopApplication(
                local_app_data=Path(directory),
                smoke_test=True,
            ).run()
    return DesktopApplication(
        local_app_data=arguments.local_app_data,
        smoke_test=arguments.smoke_test,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
