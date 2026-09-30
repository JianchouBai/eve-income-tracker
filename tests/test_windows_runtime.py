import pytest

from app.windows_runtime import (
    APP_TITLE,
    ERROR_ALREADY_EXISTS,
    FILEMUSTEXIST,
    IDYES,
    MB_ICONERROR,
    MB_YESNO,
    PATHMUSTEXIST,
    WEBVIEW2_MACHINE_64_PATH,
    WEBVIEW2_USER_PATH,
    SingleInstanceGuard,
    WindowsRuntimeError,
    ask_yes_no,
    choose_legacy_database,
    show_native_error,
    webview2_version,
)


class FakeKernel:
    def __init__(self, *, handle=101, last_error=0):
        self.handle = handle
        self.error = last_error
        self.created = []
        self.closed = []

    def create_mutex(self, name):
        self.created.append(name)
        return self.handle

    def last_error(self):
        return self.error

    def close_handle(self, handle):
        self.closed.append(handle)


class FakeRegistry:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def read_string(self, hive, path, name):
        self.calls.append((hive, path, name))
        return self.values.get((hive, path, name))


class FakeDialog:
    def __init__(self, *, answer=IDYES, selected=None):
        self.answer = answer
        self.selected = selected
        self.messages = []
        self.file_requests = []

    def message_box(self, title, message, flags):
        self.messages.append((title, message, flags))
        return self.answer

    def choose_file(self, title, file_filter, flags):
        self.file_requests.append((title, file_filter, flags))
        return self.selected


def test_single_instance_guard_closes_only_an_owned_handle():
    kernel = FakeKernel()
    guard = SingleInstanceGuard(kernel=kernel)

    assert guard.acquire() is True
    assert guard.acquire() is True
    assert kernel.created == [guard.name]
    assert kernel.closed == []

    guard.release()
    guard.release()
    assert kernel.closed == [101]


def test_single_instance_guard_closes_duplicate_handle_and_reports_already_running():
    kernel = FakeKernel(last_error=ERROR_ALREADY_EXISTS)
    guard = SingleInstanceGuard(kernel=kernel)

    assert guard.acquire() is False
    assert kernel.closed == [101]
    guard.release()
    assert kernel.closed == [101]


def test_single_instance_guard_reports_creation_failure_without_owning_handle():
    kernel = FakeKernel(handle=0, last_error=5)
    guard = SingleInstanceGuard(kernel=kernel)

    with pytest.raises(WindowsRuntimeError) as caught:
        guard.acquire()

    assert caught.value.code == "mutex_create_failed"
    assert kernel.closed == []


def test_webview2_version_checks_official_machine_and_user_locations():
    values = {
        ("HKLM", WEBVIEW2_MACHINE_64_PATH, "pv"): "0.0.0.0",
        ("HKCU", WEBVIEW2_USER_PATH, "pv"): "123.0.2420.97",
    }
    registry = FakeRegistry(values)

    assert webview2_version(registry=registry, is_64bit=True) == "123.0.2420.97"
    assert registry.calls == [
        ("HKLM", WEBVIEW2_MACHINE_64_PATH, "pv"),
        ("HKCU", WEBVIEW2_USER_PATH, "pv"),
    ]


@pytest.mark.parametrize("version", [None, "", "0.0.0.0", "not-a-version", "1.-1.0"])
def test_webview2_version_rejects_missing_zero_or_malformed_values(version):
    registry = FakeRegistry(
        {
            ("HKLM", WEBVIEW2_MACHINE_64_PATH, "pv"): version,
            ("HKCU", WEBVIEW2_USER_PATH, "pv"): None,
        }
    )

    assert webview2_version(registry=registry, is_64bit=True) is None


def test_native_prompts_use_fixed_application_title_and_expected_styles():
    dialog = FakeDialog()

    show_native_error("无法启动本地服务。", dialog=dialog)
    assert ask_yes_no("是否导入旧版数据库？", dialog=dialog) is True

    error_prompt, question_prompt = dialog.messages
    assert error_prompt[0] == APP_TITLE
    assert error_prompt[1] == "无法启动本地服务。"
    assert error_prompt[2] & MB_ICONERROR
    assert question_prompt[0] == APP_TITLE
    assert question_prompt[2] & MB_YESNO


def test_legacy_database_picker_accepts_existing_file_and_handles_cancel(tmp_path):
    database = tmp_path / "旧版 收益.sqlite3"
    database.write_bytes(b"SQLite format 3\x00")
    selected = FakeDialog(selected=str(database))

    assert choose_legacy_database(dialog=selected) == database.resolve()
    request = selected.file_requests[-1]
    assert "SQLite" in request[1]
    assert request[2] & FILEMUSTEXIST
    assert request[2] & PATHMUSTEXIST
    assert choose_legacy_database(dialog=FakeDialog(selected=None)) is None


def test_legacy_database_picker_rejects_adapter_path_that_is_not_a_file(tmp_path):
    missing = tmp_path / "missing.sqlite3"

    with pytest.raises(WindowsRuntimeError) as caught:
        choose_legacy_database(dialog=FakeDialog(selected=str(missing)))

    assert caught.value.code == "legacy_database_invalid"
    assert str(missing) not in repr(caught.value)
