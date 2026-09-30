from __future__ import annotations

import ctypes
import os
import tempfile
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet

from app.security import validate_fernet_key


class DataProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


class KeyStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(wintypes.BYTE)),
    ]


class WindowsDpapiProtector:
    _UI_FORBIDDEN = 0x01

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows DPAPI is unavailable")

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._protect = crypt32.CryptProtectData
        self._protect.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._protect.restype = wintypes.BOOL

        self._unprotect = crypt32.CryptUnprotectData
        self._unprotect.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._unprotect.restype = wintypes.BOOL

        self._local_free = kernel32.LocalFree
        self._local_free.argtypes = [ctypes.c_void_p]
        self._local_free.restype = ctypes.c_void_p

    @staticmethod
    def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = (wintypes.BYTE * len(data)).from_buffer_copy(data)
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)),
        )
        return blob, buffer

    def _transform(self, operation, data: bytes) -> bytes:
        input_blob, input_buffer = self._input_blob(data)
        output_blob = _DataBlob()
        ctypes.set_last_error(0)
        succeeded = operation(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._local_free(ctypes.cast(output_blob.pbData, ctypes.c_void_p))

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(self._protect, plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        return self._transform(self._unprotect, protected)


class FernetKeyStore:
    def __init__(
        self,
        path: Path,
        protector: DataProtector,
        *,
        key_factory: Callable[[], bytes] = Fernet.generate_key,
    ) -> None:
        self._path = Path(path)
        self._protector = protector
        self._key_factory = key_factory

    def load_or_create(self) -> bytes:
        try:
            exists = self._path.exists()
        except OSError:
            raise KeyStoreError("key_unreadable") from None

        if exists:
            return self._load_existing()
        return self._create()

    def _load_existing(self) -> bytes:
        try:
            protected = self._path.read_bytes()
        except OSError:
            raise KeyStoreError("key_unreadable") from None

        try:
            plaintext = self._protector.unprotect(protected)
            return validate_fernet_key(plaintext)
        except Exception:  # noqa: BLE001 - sanitize backend failure details
            raise KeyStoreError("key_corrupt") from None

    def _create(self) -> bytes:
        try:
            key = validate_fernet_key(self._key_factory())
        except Exception:  # noqa: BLE001 - sanitize backend failure details
            raise KeyStoreError("key_generation_failed") from None

        try:
            protected = self._protector.protect(key)
            if not isinstance(protected, bytes) or not protected:
                raise ValueError
        except Exception:  # noqa: BLE001 - sanitize backend failure details
            raise KeyStoreError("key_protection_failed") from None

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise KeyStoreError("key_unwritable") from None

        temporary_path: Path | None = None
        try:
            fd, raw_temporary_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temporary_path = Path(raw_temporary_path)
            with os.fdopen(fd, "wb") as temporary_file:
                temporary_file.write(protected)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            try:
                os.link(temporary_path, self._path)
            except FileExistsError:
                return self._load_existing()
            return key
        except KeyStoreError:
            raise
        except OSError:
            raise KeyStoreError("key_unwritable") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
