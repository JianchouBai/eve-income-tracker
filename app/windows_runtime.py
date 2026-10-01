from __future__ import annotations

import ctypes
import os
import platform
import re
from pathlib import Path
from typing import ClassVar, Protocol, Self

APP_TITLE = "EVE 收益追踪器"
MUTEX_NAME = r"Local\EVEIncomeTracker.SingleInstance"
ERROR_ALREADY_EXISTS = 183

MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONERROR = 0x00000010
MB_ICONQUESTION = 0x00000020
MB_SETFOREGROUND = 0x00010000
IDYES = 6

FILEMUSTEXIST = 0x00001000
PATHMUSTEXIST = 0x00000800
OFN_EXPLORER = 0x00080000
OFN_NOCHANGEDIR = 0x00000008

WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_MACHINE_64_PATH = (
    rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"
)
WEBVIEW2_MACHINE_32_PATH = (
    rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"
)
WEBVIEW2_USER_PATH = rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"


class WindowsRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class KernelAdapter(Protocol):
    def create_mutex(self, name: str) -> int: ...

    def last_error(self) -> int: ...

    def close_handle(self, handle: int) -> None: ...


class RegistryAdapter(Protocol):
    def read_string(self, hive: str, path: str, name: str) -> str | None: ...


class DialogAdapter(Protocol):
    def message_box(self, title: str, message: str, flags: int) -> int: ...

    def choose_file(self, title: str, file_filter: str, flags: int) -> str | None: ...


class _Kernel32Adapter:
    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsRuntimeError("windows_required")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        self._kernel32 = kernel32

    def create_mutex(self, name: str) -> int:
        ctypes.set_last_error(0)
        return int(self._kernel32.CreateMutexW(None, False, name) or 0)

    def last_error(self) -> int:
        return ctypes.get_last_error()

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(ctypes.c_void_p(handle))


class _WinRegistryAdapter:
    def read_string(self, hive: str, path: str, name: str) -> str | None:
        if os.name != "nt":
            return None
        import winreg

        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, path) as key:
                value, value_type = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
            return None
        return value if isinstance(value, str) else None


class _OpenFileNameW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", ctypes.c_uint32),
        ("hwndOwner", ctypes.c_void_p),
        ("hInstance", ctypes.c_void_p),
        ("lpstrFilter", ctypes.c_wchar_p),
        ("lpstrCustomFilter", ctypes.c_wchar_p),
        ("nMaxCustFilter", ctypes.c_uint32),
        ("nFilterIndex", ctypes.c_uint32),
        ("lpstrFile", ctypes.c_wchar_p),
        ("nMaxFile", ctypes.c_uint32),
        ("lpstrFileTitle", ctypes.c_wchar_p),
        ("nMaxFileTitle", ctypes.c_uint32),
        ("lpstrInitialDir", ctypes.c_wchar_p),
        ("lpstrTitle", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("nFileOffset", ctypes.c_uint16),
        ("nFileExtension", ctypes.c_uint16),
        ("lpstrDefExt", ctypes.c_wchar_p),
        ("lCustData", ctypes.c_ssize_t),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_wchar_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_uint32),
        ("FlagsEx", ctypes.c_uint32),
    ]


class _NativeDialogAdapter:
    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsRuntimeError("windows_required")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        self._user32.MessageBoxW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        self._user32.MessageBoxW.restype = ctypes.c_int
        self._comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OpenFileNameW)]
        self._comdlg32.GetOpenFileNameW.restype = ctypes.c_bool
        self._comdlg32.CommDlgExtendedError.argtypes = []
        self._comdlg32.CommDlgExtendedError.restype = ctypes.c_uint32

    def message_box(self, title: str, message: str, flags: int) -> int:
        return int(self._user32.MessageBoxW(None, message, title, flags))

    def choose_file(self, title: str, file_filter: str, flags: int) -> str | None:
        buffer = ctypes.create_unicode_buffer(32768)
        request = _OpenFileNameW()
        request.lStructSize = ctypes.sizeof(_OpenFileNameW)
        request.lpstrFilter = file_filter
        request.nFilterIndex = 1
        request.lpstrFile = ctypes.cast(buffer, ctypes.c_wchar_p)
        request.nMaxFile = len(buffer)
        request.lpstrTitle = title
        request.Flags = flags
        request.lpstrDefExt = "sqlite3"
        if self._comdlg32.GetOpenFileNameW(ctypes.byref(request)):
            return buffer.value
        if self._comdlg32.CommDlgExtendedError() != 0:
            raise WindowsRuntimeError("file_dialog_failed")
        return None


class SingleInstanceGuard:
    def __init__(
        self,
        name: str = MUTEX_NAME,
        *,
        kernel: KernelAdapter | None = None,
    ) -> None:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("mutex name is invalid")
        self.name = name
        self._kernel = kernel or _Kernel32Adapter()
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle = self._kernel.create_mutex(self.name)
        error = self._kernel.last_error()
        if not handle:
            raise WindowsRuntimeError("mutex_create_failed")
        if error == ERROR_ALREADY_EXISTS:
            self._kernel.close_handle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel.close_handle(handle)

    def __enter__(self) -> Self:
        if not self.acquire():
            raise WindowsRuntimeError("already_running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _version_key(value: str | None) -> tuple[int, ...] | None:
    if not isinstance(value, str) or re.fullmatch(r"\d+(?:\.\d+){3}", value) is None:
        return None
    parsed = tuple(int(part) for part in value.split("."))
    return parsed if parsed > (0, 0, 0, 0) else None


def webview2_version(
    *,
    registry: RegistryAdapter | None = None,
    is_64bit: bool | None = None,
) -> str | None:
    registry = registry or _WinRegistryAdapter()
    if is_64bit is None:
        is_64bit = platform.machine().lower().endswith("64")
    machine_path = WEBVIEW2_MACHINE_64_PATH if is_64bit else WEBVIEW2_MACHINE_32_PATH
    versions = []
    for hive, path in (("HKLM", machine_path), ("HKCU", WEBVIEW2_USER_PATH)):
        value = registry.read_string(hive, path, "pv")
        key = _version_key(value)
        if key is not None:
            versions.append((key, value))
    return max(versions, default=((), None))[1]


def show_native_error(
    message: str,
    *,
    dialog: DialogAdapter | None = None,
) -> None:
    (dialog or _NativeDialogAdapter()).message_box(
        APP_TITLE,
        message,
        MB_OK | MB_ICONERROR | MB_SETFOREGROUND,
    )


def ask_yes_no(
    message: str,
    *,
    dialog: DialogAdapter | None = None,
) -> bool:
    result = (dialog or _NativeDialogAdapter()).message_box(
        APP_TITLE,
        message,
        MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND,
    )
    return result == IDYES


def choose_legacy_database(
    *,
    dialog: DialogAdapter | None = None,
) -> Path | None:
    selected = (dialog or _NativeDialogAdapter()).choose_file(
        "选择要导入的旧版 EVE 收益数据库",
        (
            "SQLite 数据库 (*.sqlite3;*.sqlite;*.db)\0"
            "*.sqlite3;*.sqlite;*.db\0"
            "所有文件 (*.*)\0*.*\0\0"
        ),
        OFN_EXPLORER | OFN_NOCHANGEDIR | FILEMUSTEXIST | PATHMUSTEXIST,
    )
    if not selected:
        return None
    try:
        path = Path(selected).resolve(strict=True)
    except (OSError, RuntimeError):
        raise WindowsRuntimeError("legacy_database_invalid") from None
    if not path.is_file():
        raise WindowsRuntimeError("legacy_database_invalid")
    return path


class FolderDialogAdapter(Protocol):
    def choose_folder(self, title: str) -> str | None: ...

    def choose_option(
        self, title: str, message: str, options: tuple[tuple[int, str], ...]
    ) -> int: ...


class ProgressDialogAdapter(Protocol):
    def show_progress(self, title: str, message: str) -> None: ...

    def close_progress(self) -> None: ...


class _BrowseInfo(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", ctypes.c_void_p),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", ctypes.c_wchar_p),
        ("lpszTitle", ctypes.c_wchar_p),
        ("ulFlags", ctypes.c_uint),
        ("lpfn", ctypes.c_void_p),
        ("lParam", ctypes.c_ssize_t),
        ("iImage", ctypes.c_int),
    ]


class _TaskDialogButton(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", ctypes.c_wchar_p)]


class _TaskDialogConfig(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hwndParent", ctypes.c_void_p),
        ("hInstance", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("dwCommonButtons", ctypes.c_uint),
        ("pszWindowTitle", ctypes.c_wchar_p),
        ("pszMainIcon", ctypes.c_void_p),
        ("pszMainInstruction", ctypes.c_wchar_p),
        ("pszContent", ctypes.c_wchar_p),
        ("cButtons", ctypes.c_uint),
        ("pButtons", ctypes.POINTER(_TaskDialogButton)),
        ("nDefaultButton", ctypes.c_int),
        ("cRadioButtons", ctypes.c_uint),
        ("pRadioButtons", ctypes.c_void_p),
        ("nDefaultRadioButton", ctypes.c_int),
        ("pszVerificationText", ctypes.c_wchar_p),
        ("pszExpandedInformation", ctypes.c_wchar_p),
        ("pszExpandedControlText", ctypes.c_wchar_p),
        ("pszCollapsedControlText", ctypes.c_wchar_p),
        ("pszFooterIcon", ctypes.c_void_p),
        ("pszFooter", ctypes.c_wchar_p),
        ("pfCallback", ctypes.c_void_p),
        ("lpCallbackData", ctypes.c_ssize_t),
        ("cxWidth", ctypes.c_uint),
    ]


class _NativeLocationDialog:
    """Small Win32 adapters; all handles stay local to the calling UI thread."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsRuntimeError("windows_required")
        self._progress_window = None
        self._progress_text = None

    def choose_folder(self, title: str) -> str | None:
        ole32 = ctypes.OleDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ole32.CoInitializeEx.restype = ctypes.c_long
        # STA is required by the shell folder browser.
        ole32.CoInitializeEx(None, 2)
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BrowseInfo)]
            shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
            shell32.SHGetPathFromIDListEx.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            shell32.SHGetPathFromIDListEx.restype = ctypes.c_bool
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree.restype = None
            display = ctypes.create_unicode_buffer(32768)
            request = _BrowseInfo()
            request.pszDisplayName = ctypes.cast(display, ctypes.c_wchar_p)
            request.lpszTitle = title
            # Filesystem directories only, new dialog style, no new-folder button.
            request.ulFlags = 0x0001 | 0x0040 | 0x0200
            item = shell32.SHBrowseForFolderW(ctypes.byref(request))
            if not item:
                return None
            try:
                path = ctypes.create_unicode_buffer(32768)
                if not shell32.SHGetPathFromIDListEx(item, path, len(path), 0):
                    raise WindowsRuntimeError("folder_dialog_failed")
                return path.value
            finally:
                ole32.CoTaskMemFree(item)
        finally:
            ole32.CoUninitialize()

    def choose_option(
        self, title: str, message: str, options: tuple[tuple[int, str], ...]
    ) -> int:
        controls = ctypes.WinDLL("comctl32", use_last_error=True)
        task_dialog = controls.TaskDialogIndirect
        task_dialog.argtypes = [
            ctypes.POINTER(_TaskDialogConfig),
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        task_dialog.restype = ctypes.c_long
        buttons = (_TaskDialogButton * len(options))(
            *(_TaskDialogButton(number, label) for number, label in options)
        )
        config = _TaskDialogConfig()
        config.cbSize = ctypes.sizeof(config)
        config.dwFlags = 0x0008  # Allow window close/Escape cancellation.
        config.pszWindowTitle = title
        config.pszContent = message
        config.cButtons = len(options)
        config.pButtons = buttons
        config.nDefaultButton = options[0][0]
        selected = ctypes.c_int()
        if task_dialog(ctypes.byref(config), ctypes.byref(selected), None, None) < 0:
            raise WindowsRuntimeError("location_dialog_failed")
        return selected.value

    def _progress_api(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.argtypes = [
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.SetWindowTextW.restype = ctypes.c_bool
        user32.UpdateWindow.argtypes = [ctypes.c_void_p]
        user32.UpdateWindow.restype = ctypes.c_bool
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        user32.DestroyWindow.restype = ctypes.c_bool
        return user32

    def show_progress(self, title: str, message: str) -> None:
        user32 = self._progress_api()
        if self._progress_window is None:
            # A noninteractive native captioned window; no close button while
            # the transaction is running. The desktop calls show at boundaries.
            self._progress_window = user32.CreateWindowExW(
                0x00000008,
                "STATIC",
                title,
                0x10C00000,
                160,
                160,
                520,
                140,
                None,
                None,
                None,
                None,
            )
            if not self._progress_window:
                self._progress_window = None
                raise WindowsRuntimeError("progress_failed")
            self._progress_text = user32.CreateWindowExW(
                0,
                "STATIC",
                message,
                0x50000000,
                24,
                30,
                460,
                48,
                self._progress_window,
                None,
                None,
                None,
            )
            if not self._progress_text:
                self.close_progress()
                raise WindowsRuntimeError("progress_failed")
        if not user32.SetWindowTextW(self._progress_text, message):
            raise WindowsRuntimeError("progress_failed")
        user32.UpdateWindow(self._progress_window)
        user32.UpdateWindow(self._progress_text)

    def close_progress(self) -> None:
        window, self._progress_window = self._progress_window, None
        self._progress_text = None
        if window and not self._progress_api().DestroyWindow(window):
            raise WindowsRuntimeError("progress_failed")


def _pick_folder(title: str, dialog: FolderDialogAdapter | None) -> Path | None:
    try:
        selected = (dialog or _NativeLocationDialog()).choose_folder(title)
        if selected is None or selected == "":
            return None
        if not isinstance(selected, str) or "\0" in selected:
            raise WindowsRuntimeError("folder_dialog_failed")
        path = Path(selected)
        if not path.is_absolute():
            raise WindowsRuntimeError("folder_dialog_failed")
        # Do not resolve links here: the validator must see any reparse component.
        return path
    except WindowsRuntimeError:
        raise
    except (OSError, ValueError, RuntimeError, AttributeError):
        raise WindowsRuntimeError("folder_dialog_failed") from None


def choose_data_parent(*, dialog: FolderDialogAdapter | None = None) -> Path | None:
    return _pick_folder("选择数据存储的父目录", dialog)


def choose_existing_data_root(
    *, dialog: FolderDialogAdapter | None = None
) -> Path | None:
    return _pick_folder("定位已有 EVE 收益数据目录", dialog)


def _location_choice(message, options, dialog):
    try:
        result = (dialog or _NativeLocationDialog()).choose_option(
            APP_TITLE, message, options
        )
        if result not in {2, *(number for number, _ in options)}:
            raise WindowsRuntimeError("location_dialog_failed")
        return result
    except WindowsRuntimeError:
        raise
    except (OSError, ValueError, RuntimeError, AttributeError):
        raise WindowsRuntimeError("location_dialog_failed") from None


def ask_retry_locate_exit(*, dialog: FolderDialogAdapter | None = None) -> str:
    result = _location_choice(
        "数据目录不可用或校验失败。请连接数据盘后重试，或定位已有数据目录。",
        ((101, "重试"), (102, "定位已有数据目录"), (103, "退出")),
        dialog,
    )
    return {101: "retry", 102: "locate", 103: "exit", 2: "exit"}[result]


class NativeStartupInteraction:
    def __init__(self, *, dialog: FolderDialogAdapter | None = None) -> None:
        self._dialog = dialog

    def choose_initial_location(self, default_root: Path) -> str:
        result = _location_choice(
            f"选择数据存储位置。默认目录：\n{default_root}",
            ((101, "使用默认位置"), (102, "选择自定义位置"), (103, "退出")),
            self._dialog,
        )
        return {101: "default", 102: "custom", 103: "exit", 2: "exit"}[result]

    def choose_legacy_action(self, root: Path) -> str:
        result = _location_choice(
            f"发现已有数据：\n{root}\n可以保留原位置或迁移到其他位置。",
            ((101, "保留原位置"), (102, "迁移到新位置"), (103, "退出")),
            self._dialog,
        )
        return {101: "keep", 102: "migrate", 103: "exit", 2: "exit"}[result]

    def choose_data_parent(self) -> Path | None:
        return choose_data_parent(dialog=self._dialog)

    def choose_existing_data_root(self) -> Path | None:
        return choose_existing_data_root(dialog=self._dialog)

    def ask_retry_locate_exit(self, code: str) -> str:
        return ask_retry_locate_exit(dialog=self._dialog)

    def confirm_location(self, root: Path, estimate=None) -> bool:
        message = f"将使用以下完整数据目录：\n{root}"
        if estimate is not None:
            message += (
                f"\n迁移预计占用：{estimate.copy_bytes:,} 字节"
                f"\n恢复副本预计占用：{estimate.recovery_bytes:,} 字节"
                f"\n目标可用空间：{estimate.free_bytes:,} 字节"
                "\n迁移验证成功后清理原数据目录。默认创建一份迁移恢复副本。"
            )
        return (
            _location_choice(message, ((101, "确认"), (102, "取消")), self._dialog)
            == 101
        )


class MigrationProgress:
    STAGES: ClassVar[dict[str, str]] = {
        "preparing": "正在准备数据迁移…",
        "copying": "正在复制数据…",
        "validating": "正在验证数据库和密钥…",
        "switching": "正在切换数据位置…",
        "cleaning": "正在清理原数据目录…",
        "complete": "数据迁移完成。",
    }

    def __init__(self, *, dialog: ProgressDialogAdapter | None = None) -> None:
        self._dialog = dialog
        self._closed = False

    def show(self, stage: str) -> None:
        if self._closed:
            raise WindowsRuntimeError("progress_closed")
        if stage not in self.STAGES:
            raise WindowsRuntimeError("progress_stage_invalid")
        try:
            if self._dialog is None:
                self._dialog = _NativeLocationDialog()
            self._dialog.show_progress(APP_TITLE, self.STAGES[stage])
        except WindowsRuntimeError:
            raise
        except (OSError, ValueError, RuntimeError, AttributeError):
            raise WindowsRuntimeError("progress_failed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._dialog is not None:
                self._dialog.close_progress()
        except (OSError, ValueError, RuntimeError, AttributeError):
            raise WindowsRuntimeError("progress_failed") from None
