import pytest

from app.desktop_bridge import (
    AuthorizationUrlError,
    DesktopBridge,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://login.eveonline.com/v2/oauth/authorize?state=a",
        "https://login.eveonline.com:443/v2/oauth/authorize?state=a",
        "https://LOGIN.EVEONLINE.COM/v2/oauth/authorize?state=a",
    ],
)
def test_bridge_opens_only_exact_eve_https_authorization_host(url):
    opened = []
    bridge = DesktopBridge(opener=lambda target: opened.append(target) or True)

    assert bridge.open_authorization_url(url) is True
    assert opened == [url]


@pytest.mark.parametrize(
    "url",
    [
        "http://login.eveonline.com/v2/oauth/authorize",
        "https://login.eveonline.com.evil.example/v2/oauth/authorize",
        "https://evil-login.eveonline.com/v2/oauth/authorize",
        "https://login.eveonline.com:444/v2/oauth/authorize",
        "https://user@login.eveonline.com/v2/oauth/authorize",
        "https://login.eveonline.com /v2/oauth/authorize",
        " https://login.eveonline.com/v2/oauth/authorize",
        "https://localhost:8000/auth/callback",
        "file:///C:/Windows/System32/calc.exe",
        "javascript:alert(1)",
        "shell:AppsFolder",
        "//login.eveonline.com/v2/oauth/authorize",
        "",
    ],
)
def test_bridge_rejects_non_allowlisted_urls_before_calling_opener(url):
    opened = []
    bridge = DesktopBridge(opener=lambda target: opened.append(target) or True)

    with pytest.raises(AuthorizationUrlError) as caught:
        bridge.open_authorization_url(url)

    assert caught.value.code == "authorization_url_rejected"
    assert opened == []
    if url:
        assert url not in repr(caught.value)


def test_bridge_converts_browser_failure_to_safe_error():
    private_url = "https://login.eveonline.com/v2/oauth/authorize?state=private-state"

    def fail(_url):
        raise RuntimeError("private-browser-detail")

    bridge = DesktopBridge(opener=fail)
    with pytest.raises(AuthorizationUrlError) as caught:
        bridge.open_authorization_url(private_url)

    rendered = f"{caught.value!s} {caught.value!r}"
    assert caught.value.code == "browser_open_failed"
    assert "private-" not in rendered


def test_bridge_exposes_no_file_picker_or_general_shell_method():
    public_methods = {name for name in dir(DesktopBridge) if not name.startswith("_")}

    assert public_methods == {
        "open_authorization_url",
        "choose_data_parent",
        "open_data_directory",
        "restart_for_migration",
    }


from pathlib import Path
from types import SimpleNamespace

from app.desktop_control import DesktopControl


class LocationFake:
    def __init__(self):
        self.current = Path("D:/EVE Income Tracker")
        self.pending = None
        self.failure = False

    def status(self):
        if self.failure:
            raise RuntimeError("private-path-key")
        return SimpleNamespace(
            current_path=self.current, pending_migration=self.pending
        )


def test_storage_bridge_revalidates_current_directory_and_gates_restart():
    location = LocationFake()
    control = DesktopControl()
    opened = []
    bridge = DesktopBridge(
        data_location=location,
        desktop_control=control,
        directory_picker=lambda: Path("E:/"),
        directory_opener=opened.append,
    )
    assert bridge.choose_data_parent() == str(Path("E:/"))
    assert bridge.open_data_directory() is True
    location.current = Path("E:/EVE Income Tracker")
    assert bridge.open_data_directory() is True
    assert opened == [Path("D:/EVE Income Tracker"), location.current]
    with pytest.raises(RuntimeError, match="migration_not_pending"):
        bridge.restart_for_migration()
    assert control.consume_restart() is False
    location.pending = object()
    assert bridge.restart_for_migration() is True
    assert control.consume_restart() is True
    location.failure = True
    for action in (bridge.open_data_directory, bridge.restart_for_migration):
        with pytest.raises(RuntimeError) as error:
            action()
        assert "private-" not in str(error.value)
    assert len(opened) == 2
    assert control.consume_restart() is False


def test_storage_bridge_cancel_and_argument_rejection():
    bridge = DesktopBridge(directory_picker=lambda: None)
    assert bridge.choose_data_parent() is None
    for action in (
        bridge.choose_data_parent,
        bridge.open_data_directory,
        bridge.restart_for_migration,
    ):
        with pytest.raises(TypeError):
            action("C:/Windows")

    def fail():
        raise OSError("private-path-key")

    bridge = DesktopBridge(directory_picker=fail)
    with pytest.raises(RuntimeError) as error:
        bridge.choose_data_parent()
    assert "private-" not in str(error.value)


def test_directory_open_false_result_is_a_safe_failure():
    bridge = DesktopBridge(
        data_location=LocationFake(), directory_opener=lambda path: False
    )
    with pytest.raises(RuntimeError, match="directory_open_failed"):
        bridge.open_data_directory()
