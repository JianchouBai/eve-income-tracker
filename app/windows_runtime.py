from __future__ import annotations

import ctypes
import os
import platform
import re
from pathlib import Path
from typing import Protocol, Self

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
