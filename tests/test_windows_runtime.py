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


class FolderDialog:
    def __init__(self, selected=None, answer=101, fail=False):
        self.selected, self.answer, self.fail = selected, answer, fail
        self.requests = []

    def choose_folder(self, title):
        self.requests.append(title)
        if self.fail:
            raise OSError("secret native details")
        return self.selected

    def choose_option(self, title, message, options):
        self.requests.append((title, message, options))
        if self.fail:
            raise OSError("secret native details")
        return self.answer


@pytest.mark.parametrize(
    "picker,title",
    [
        ("choose_data_parent", "选择数据存储的父目录"),
        ("choose_existing_data_root", "定位已有 EVE 收益数据目录"),
    ],
)
def test_folder_picker_returns_path_and_cancellation_without_creating(
    tmp_path, picker, title
):
    from app import windows_runtime as runtime

    dialog = FolderDialog(selected=str(tmp_path))
    assert getattr(runtime, picker)(dialog=dialog) == tmp_path
    assert dialog.requests == [title]
    assert getattr(runtime, picker)(dialog=FolderDialog()) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("picker", ["choose_data_parent", "choose_existing_data_root"])
def test_folder_picker_sanitizes_api_failure(picker):
    from app import windows_runtime as runtime

    with pytest.raises(WindowsRuntimeError, match="folder_dialog_failed") as caught:
        getattr(runtime, picker)(dialog=FolderDialog(fail=True))
    assert "secret" not in repr(caught.value)


@pytest.mark.parametrize(
    "answer,expected", [(101, "retry"), (102, "locate"), (103, "exit"), (2, "exit")]
)
def test_retry_locate_exit_uses_fixed_options(answer, expected):
    from app.windows_runtime import ask_retry_locate_exit

    dialog = FolderDialog(answer=answer)
    assert ask_retry_locate_exit(dialog=dialog) == expected
    assert dialog.requests[0][0] == APP_TITLE
    assert dialog.requests[0][2] == (
        (101, "重试"),
        (102, "定位已有数据目录"),
        (103, "退出"),
    )


def test_native_option_failure_and_unknown_result_are_stable():
    from app.windows_runtime import ask_retry_locate_exit

    for dialog in (FolderDialog(fail=True), FolderDialog(answer=999)):
        with pytest.raises(WindowsRuntimeError, match="location_dialog_failed"):
            ask_retry_locate_exit(dialog=dialog)


def test_native_folder_api_on_non_windows_is_stable(monkeypatch):
    from app import windows_runtime as runtime

    monkeypatch.setattr(runtime.os, "name", "posix")
    with pytest.raises(WindowsRuntimeError, match="windows_required"):
        runtime.choose_data_parent()


def test_progress_stages_close_and_failures_are_stable():
    from app.windows_runtime import MigrationProgress

    class ProgressDialog:
        def __init__(self):
            self.stages, self.closed = [], 0

        def show_progress(self, title, message):
            self.stages.append((title, message))

        def close_progress(self):
            self.closed += 1

    dialog = ProgressDialog()
    progress = MigrationProgress(dialog=dialog)
    for stage in (
        "preparing",
        "copying",
        "validating",
        "switching",
        "cleaning",
        "complete",
    ):
        progress.show(stage)
    assert len(dialog.stages) == 6
    assert all(title == APP_TITLE for title, _ in dialog.stages)
    with pytest.raises(WindowsRuntimeError, match="progress_stage_invalid"):
        progress.show("secret")
    progress.close()
    progress.close()
    assert dialog.closed == 1
    with pytest.raises(WindowsRuntimeError, match="progress_closed"):
        progress.show("copying")

    class BrokenProgress:
        def show_progress(self, *args):
            raise OSError("secret progress data")

        def close_progress(self):
            raise OSError("secret progress data")

    broken = MigrationProgress(dialog=BrokenProgress())
    with pytest.raises(WindowsRuntimeError, match="progress_failed"):
        broken.show("copying")
    with pytest.raises(WindowsRuntimeError, match="progress_failed"):
        broken.close()
    broken.close()


@pytest.mark.parametrize(
    "answer,expected", [(101, "default"), (102, "custom"), (103, "exit"), (2, "exit")]
)
def test_native_startup_fresh_choice_and_final_path_preview(tmp_path, answer, expected):
    from app.windows_runtime import NativeStartupInteraction

    dialog = FolderDialog(answer=answer)
    interaction = NativeStartupInteraction(dialog=dialog)
    root = tmp_path / "EVE Income Tracker"
    assert interaction.choose_initial_location(root) == expected
    assert str(root) in dialog.requests[0][1]
    if answer in (101, 102, 2):
        assert interaction.confirm_location(root) is (answer == 101)


@pytest.mark.parametrize(
    "answer,expected", [(101, "keep"), (102, "migrate"), (103, "exit"), (2, "exit")]
)
def test_native_startup_legacy_choice(tmp_path, answer, expected):
    from app.windows_runtime import NativeStartupInteraction

    dialog = FolderDialog(answer=answer)
    assert (
        NativeStartupInteraction(dialog=dialog).choose_legacy_action(tmp_path)
        == expected
    )
    assert str(tmp_path) in dialog.requests[0][1]


def test_native_migration_confirmation_exposes_space_and_cleanup(tmp_path):
    from app.data_migration import MigrationEstimate
    from app.windows_runtime import NativeStartupInteraction

    dialog = FolderDialog(answer=101, selected=str(tmp_path))
    interaction = NativeStartupInteraction(dialog=dialog)
    root = tmp_path / "EVE Income Tracker"
    assert interaction.confirm_location(
        root, MigrationEstimate(root, 1234, 5678, 99999)
    )
    message = dialog.requests[-1][1]
    assert str(root) in message
    assert all(
        value in message for value in ("1,234", "5,678", "99,999", "清理原数据目录")
    )
    assert interaction.choose_data_parent() == tmp_path
    assert interaction.choose_existing_data_root() == tmp_path
    assert interaction.ask_retry_locate_exit("secret") == "retry"
    assert "secret" not in dialog.requests[-1][1]
