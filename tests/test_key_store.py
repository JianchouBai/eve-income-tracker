import importlib
import importlib.util
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

KEY = b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def key_store_module():
    assert importlib.util.find_spec("app.key_store") is not None, (
        "app.key_store is missing"
    )
    return importlib.import_module("app.key_store")


class PrefixProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"protected:"):
            raise ValueError("private-protector-details")
        return protected.removeprefix(b"protected:")[::-1]


def test_key_is_generated_protected_and_reused(tmp_path):
    module = key_store_module()
    path = tmp_path / "用户 配置" / "local.key"
    store = module.FernetKeyStore(path, PrefixProtector(), key_factory=lambda: KEY)

    first = store.load_or_create()
    disk_bytes = path.read_bytes()
    second = module.FernetKeyStore(
        path,
        PrefixProtector(),
        key_factory=lambda: (_ for _ in ()).throw(AssertionError("regenerated")),
    ).load_or_create()

    assert first == KEY
    assert second == KEY
    assert disk_bytes == b"protected:" + KEY[::-1]
    assert KEY not in disk_bytes
    Fernet(first)


def test_independent_key_paths_do_not_share_key_material(tmp_path):
    module = key_store_module()
    first_key = Fernet.generate_key()
    second_key = Fernet.generate_key()

    first = module.FernetKeyStore(
        tmp_path / "user-a" / "local.key",
        PrefixProtector(),
        key_factory=lambda: first_key,
    ).load_or_create()
    second = module.FernetKeyStore(
        tmp_path / "user-b" / "local.key",
        PrefixProtector(),
        key_factory=lambda: second_key,
    ).load_or_create()

    assert first == first_key
    assert second == second_key
    assert first != second


def test_concurrent_first_create_reuses_winning_key_without_overwrite(
    tmp_path, monkeypatch
):
    module = key_store_module()
    path = tmp_path / "local.key"
    losing_key = Fernet.generate_key()
    winning_key = Fernet.generate_key()

    def another_instance_wins(source, destination):
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        path.write_bytes(b"protected:" + winning_key[::-1])
        raise FileExistsError

    monkeypatch.setattr(module.os, "link", another_instance_wins)

    loaded = module.FernetKeyStore(
        path,
        PrefixProtector(),
        key_factory=lambda: losing_key,
    ).load_or_create()

    assert loaded == winning_key
    assert path.read_bytes() == b"protected:" + winning_key[::-1]
    assert losing_key not in path.read_bytes()
    assert list(path.parent.glob("*.tmp")) == []


def test_first_create_flushes_temporary_file_before_install(tmp_path, monkeypatch):
    module = key_store_module()
    path = tmp_path / "nested" / "local.key"
    real_mkstemp = module.tempfile.mkstemp
    real_fsync = module.os.fsync
    observed = {"same_directory": False, "fsync": False}

    def tracked_mkstemp(*args, **kwargs):
        observed["same_directory"] = Path(kwargs["dir"]) == path.parent
        return real_mkstemp(*args, **kwargs)

    def tracked_fsync(fd):
        observed["fsync"] = True
        return real_fsync(fd)

    monkeypatch.setattr(module.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(module.os, "fsync", tracked_fsync)

    module.FernetKeyStore(
        path,
        PrefixProtector(),
        key_factory=lambda: KEY,
    ).load_or_create()

    assert observed == {"same_directory": True, "fsync": True}
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("contents", "protector", "expected_code"),
    [
        (b"", PrefixProtector(), "key_corrupt"),
        (b"private-corrupt-protected-bytes", PrefixProtector(), "key_corrupt"),
        (b"protected:not-a-fernet-key", PrefixProtector(), "key_corrupt"),
    ],
)
def test_corrupt_key_is_rejected_without_replacement_or_disclosure(
    tmp_path, contents, protector, expected_code
):
    module = key_store_module()
    path = tmp_path / "local.key"
    path.write_bytes(contents)

    with pytest.raises(module.KeyStoreError) as caught:
        module.FernetKeyStore(
            path, protector, key_factory=Fernet.generate_key
        ).load_or_create()

    assert caught.value.code == expected_code
    assert path.read_bytes() == contents
    rendered = f"{caught.value!s} {caught.value!r}"
    assert "private-" not in rendered
    assert str(path) not in rendered


def test_unreadable_existing_key_reports_safe_error_without_replacement(
    tmp_path, monkeypatch
):
    module = key_store_module()
    path = tmp_path / "local.key"
    original = b"protected:" + KEY[::-1]
    path.write_bytes(original)
    original_read_bytes = Path.read_bytes

    def denied(candidate):
        if candidate == path:
            raise PermissionError("private-path-details")
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", denied)

    with pytest.raises(module.KeyStoreError) as caught:
        module.FernetKeyStore(path, PrefixProtector()).load_or_create()

    assert caught.value.code == "key_unreadable"
    assert original_read_bytes(path) == original
    assert "private-" not in str(caught.value)


def test_unwritable_new_key_reports_safe_error_without_partial_file(
    tmp_path, monkeypatch
):
    module = key_store_module()
    path = tmp_path / "blocked" / "local.key"

    def denied(*args, **kwargs):
        raise PermissionError("private-write-details")

    monkeypatch.setattr(module.tempfile, "mkstemp", denied)

    with pytest.raises(module.KeyStoreError) as caught:
        module.FernetKeyStore(
            path, PrefixProtector(), key_factory=lambda: KEY
        ).load_or_create()

    assert caught.value.code == "key_unwritable"
    assert not path.exists()
    assert "private-" not in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_windows_dpapi_round_trips_only_protected_bytes():
    module = key_store_module()
    protector = module.WindowsDpapiProtector()
    plaintext = b"dpapi-current-user-test-value"

    protected = protector.protect(plaintext)

    assert protected != plaintext
    assert plaintext not in protected
    assert protector.unprotect(protected) == plaintext
