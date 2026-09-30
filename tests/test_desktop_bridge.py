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

    assert public_methods == {"open_authorization_url"}
