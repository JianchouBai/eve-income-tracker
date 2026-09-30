from __future__ import annotations

import argparse
import importlib
import logging
import secrets
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.application import build_app
from app.auth_attempts import AuthorizationAttemptStore
from app.config import Settings
from app.database_lifecycle import DatabaseLifecycle, DatabaseLifecycleError
from app.desktop_bridge import DesktopBridge
from app.desktop_server import LocalServer, LocalServerError, wait_until_healthy
from app.desktop_session import DesktopLaunchTokenStore
from app.key_store import FernetKeyStore, KeyStoreError, WindowsDpapiProtector
from app.logging_setup import close_desktop_logging, configure_desktop_logging
from app.paths import (
    PROJECT_ROOT,
    AppPaths,
    ConfigurationError,
    ReleaseConfig,
    resolve_app_paths,
)
from app.storage import LATEST_SCHEMA_VERSION, Database
from app.web import RuntimeContext
from app.windows_runtime import (
    APP_TITLE,
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
    "首次使用时，是否从当前命令行版导入旧数据库？\n"
    "选择“否”或取消后，将为这台电脑创建新的独立数据库。"
)
SMOKE_SUCCESS = "DESKTOP_SMOKE_OK healthz=200 home=200 resources=ok database=ok"

_ERROR_MESSAGES = {
    "already_running": "EVE 收益追踪器已经运行，请先切换到现有窗口。",
    "mutex_create_failed": "无法建立单实例保护。请重新启动 Windows 后再试。",
    "webview_missing": (
        "未检测到 Microsoft Edge WebView2 Runtime。请重新运行安装程序以安装 WebView2。"
    ),
    "data_unwritable": (
        "无法写入本机数据目录。请检查当前 Windows 用户对 LocalAppData 的权限。"
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
        "无法创建本机数据库。请检查 LocalAppData 的可用空间与写入权限。"
    ),
    "backup_unwritable": (
        "升级前无法创建数据库备份。原数据库未修改，请检查备份目录权限。"
    ),
    "migration_failed": (
        "数据库升级失败，已尝试恢复原文件。请保留日志和备份后再重试。"
    ),
    "database_restore_failed": (
        "数据库升级失败且无法自动恢复。请停止操作并从 backups 目录恢复。"
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


@dataclass(frozen=True, slots=True)
class DesktopDependencies:
    mutex_factory: Callable[[], Any] = SingleInstanceGuard
    path_resolver: Callable[[Path | None], AppPaths] = _resolve_desktop_paths
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
    bridge_factory: Callable[[], Any] = DesktopBridge
    webview_loader: Callable[[], Any] = _load_webview
    error_shower: Callable[[str], None] = show_native_error
    session_secret_factory: Callable[[], str] = _new_session_secret
    smoke_verifier: Callable[[AppPaths, Any, str, Any], None] = _verify_smoke


def _error_code(error: BaseException) -> str:
    if isinstance(
        error,
        DesktopStartupError | LocalServerError | WindowsRuntimeError,
    ):
        return error.code
    if isinstance(error, KeyStoreError):
        return "key_failed"
    if isinstance(error, DatabaseLifecycleError):
        if error.code.startswith("import_"):
            return "import_failed"
        return error.code
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
            Path(local_app_data).resolve() if local_app_data is not None else None
        )
        self._smoke_test = smoke_test
        self._dependencies = dependencies or DesktopDependencies()

    def run(self) -> int:
        dependencies = self._dependencies
        guard = None
        server = None
        logging_active = False
        failure_code: str | None = None
        succeeded = False

        try:
            guard = dependencies.mutex_factory()
            if not guard.acquire():
                raise DesktopStartupError("already_running")

            try:
                paths = dependencies.path_resolver(self._local_app_data)
            except ConfigurationError:
                raise DesktopStartupError("data_unwritable") from None
            dependencies.logging_configurer(paths.logs_dir / DESKTOP_LOG_NAME)
            logging_active = True

            if not self._smoke_test and not dependencies.webview_checker():
                raise DesktopStartupError("webview_missing")

            database = dependencies.database_factory(paths.database_path)
            lifecycle = dependencies.lifecycle_factory(database, paths.backups_dir)
            try:
                database_exists = paths.database_path.exists()
            except OSError:
                raise DesktopStartupError("data_unwritable") from None
            if (
                not database_exists
                and not self._smoke_test
                and dependencies.question_asker(FIRST_RUN_QUESTION)
            ):
                source = dependencies.legacy_picker()
                if source is not None:
                    lifecycle.import_legacy(source)

            protector = dependencies.protector_factory()
            key = dependencies.key_store_factory(
                paths.key_path,
                protector,
            ).load_or_create()
            lifecycle.prepare()

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
                    authorization_bridge = dependencies.bridge_factory()
                    launch_url = "http://localhost:8000/?" + urlencode(
                        {"launch_token": launch_token}
                    )
                    webview.create_window(
                        APP_TITLE,
                        launch_url,
                        js_api=authorization_bridge,
                        width=1180,
                        height=780,
                        min_size=(900, 600),
                        resizable=True,
                        zoomable=False,
                    )
                    webview.start(
                        gui="edgechromium",
                        debug=False,
                        private_mode=True,
                        storage_path=str(paths.webview_storage_dir),
                    )
                except Exception:  # noqa: BLE001 - expose only fixed desktop error.
                    raise DesktopStartupError("window_failed") from None
            succeeded = True
        except Exception as error:  # noqa: BLE001 - desktop boundary sanitizes all failures.
            failure_code = _error_code(error)
        finally:
            if server is not None:
                try:
                    stopped = server.stop(timeout=3)
                except Exception:  # noqa: BLE001 - preserve original startup result.
                    stopped = False
                if not stopped and failure_code is None:
                    failure_code = "shutdown_failed"
                    succeeded = False
            if failure_code is not None and logging_active:
                logging.getLogger(__name__).warning(
                    "Desktop application failed: code=%s",
                    failure_code,
                )
            if logging_active:
                try:
                    dependencies.logging_closer()
                except Exception:  # noqa: BLE001 - expose no cleanup details.
                    logging.getLogger(__name__).warning(
                        "Desktop cleanup failed: stage=logging",
                    )
            if guard is not None:
                try:
                    guard.release()
                except Exception:  # noqa: BLE001 - expose no cleanup details.
                    logging.getLogger(__name__).warning(
                        "Desktop cleanup failed: stage=mutex",
                    )

        if failure_code is not None:
            message = _ERROR_MESSAGES.get(
                failure_code,
                _ERROR_MESSAGES["startup_failed"],
            )
            if self._smoke_test:
                print(f"DESKTOP_SMOKE_FAILED code={failure_code}", file=sys.stderr)
            else:
                try:
                    dependencies.error_shower(message)
                except Exception:  # noqa: BLE001 - no secondary dialog failure details.
                    logging.getLogger(__name__).warning(
                        "Desktop error dialog failed",
                    )
            return 1
        if self._smoke_test:
            print(SMOKE_SUCCESS)
        return 0 if succeeded else 1


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
