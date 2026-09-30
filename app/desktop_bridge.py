from __future__ import annotations

import webbrowser
from collections.abc import Callable
from urllib.parse import urlsplit


class AuthorizationUrlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class DesktopBridge:
    def __init__(
        self,
        *,
        opener: Callable[[str], bool] = webbrowser.open_new_tab,
    ) -> None:
        self._opener = opener

    def open_authorization_url(self, url: str) -> bool:
        if not self._is_allowed(url):
            raise AuthorizationUrlError("authorization_url_rejected")
        try:
            opened = self._opener(url)
        except Exception:  # noqa: BLE001 - expose no shell or browser details.
            raise AuthorizationUrlError("browser_open_failed") from None
        if opened is not True:
            raise AuthorizationUrlError("browser_open_failed")
        return True

    @staticmethod
    def _is_allowed(url: str) -> bool:
        if (
            not isinstance(url, str)
            or not url
            or any(character.isspace() for character in url)
        ):
            return False
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == "login.eveonline.com"
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )
