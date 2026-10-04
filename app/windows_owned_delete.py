"""Delete an owned Windows directory through handles, never a final path lookup."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _ByHandleInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("modified", _FileTime),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _Disposition(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_ubyte)]


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleInformation),
    )
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _open_bound_handle(kernel, path: Path, *, delete: bool = True):
    handle = kernel.CreateFileW(
        str(path),
        (_DELETE if delete else 0) | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE or handle is None:
        raise OSError(ctypes.get_last_error())
    return handle


def _information(kernel, handle) -> _ByHandleInformation:
    info = _ByHandleInformation()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError(ctypes.get_last_error())
    if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("reparse point")
    return info


def _delete_opened(
    kernel, path: Path, handle, *, marker_last: str | None = None
) -> None:
    try:
        info = _information(kernel, handle)
        if info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
            if marker_last is not None:
                children.sort(key=lambda child: child.name == marker_last)
            for child in children:
                child_handle = _open_bound_handle(kernel, child)
                _delete_opened(kernel, child, child_handle)
        disposition = _Disposition(1)
        if not kernel.SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise OSError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def delete_tree_bound(
    root: Path, expected_identity: tuple[int, int], *, marker_last: str | None = None
) -> None:
    """Refuse a swapped root; hold each object until its own deletion is marked."""
    if os.name != "nt":
        raise OSError("Windows handle deletion is unavailable")
    kernel = _kernel32()
    ancestors = []
    try:
        for ancestor in reversed(root.parents):
            ancestor_handle = _open_bound_handle(kernel, ancestor, delete=False)
            ancestors.append(ancestor_handle)
            if (
                not _information(kernel, ancestor_handle).attributes
                & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise OSError("non-directory ancestor")
        handle = _open_bound_handle(kernel, root)
        try:
            _information(kernel, handle)
            details = root.stat(follow_symlinks=False)
            if (details.st_dev, details.st_ino) != expected_identity:
                raise OSError("directory identity changed")
        except (OSError, ValueError):
            kernel.CloseHandle(handle)
            raise
        _delete_opened(kernel, root, handle, marker_last=marker_last)
    finally:
        for ancestor_handle in reversed(ancestors):
            kernel.CloseHandle(ancestor_handle)
