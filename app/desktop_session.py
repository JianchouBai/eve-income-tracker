from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

LAUNCH_TOKEN_LIFETIME = timedelta(seconds=60)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DesktopSessionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(repr=False)
class _StoredLaunch:
    token: str = field(repr=False)
    session_id: str = field(repr=False)
    expires_at: datetime


class DesktopLaunchTokenStore:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._clock = clock
        self._random_bytes = random_bytes
        self._tokens: dict[str, _StoredLaunch] = {}

    def issue(self) -> str:
        self.cleanup()
        token = self._new_token()
        self._tokens[token] = _StoredLaunch(
            token=token,
            session_id=self._encode_entropy(),
            expires_at=self._clock() + LAUNCH_TOKEN_LIFETIME,
        )
        return token

    def consume(self, supplied_token: str) -> str:
        if not isinstance(supplied_token, str) or not supplied_token:
            raise DesktopSessionError("launch_token_unknown")
        matched: _StoredLaunch | None = None
        for token, launch in tuple(self._tokens.items()):
            if secrets.compare_digest(token, supplied_token):
                matched = launch
        if matched is None:
            self.cleanup()
            raise DesktopSessionError("launch_token_unknown")
        self._tokens.pop(matched.token, None)
        if self._clock() >= matched.expires_at:
            raise DesktopSessionError("launch_token_expired")
        self.cleanup()
        return matched.session_id

    def cleanup(self) -> int:
        now = self._clock()
        expired = [
            token for token, launch in self._tokens.items() if now >= launch.expires_at
        ]
        for token in expired:
            self._tokens.pop(token, None)
        return len(expired)

    def _new_token(self) -> str:
        for _ in range(16):
            token = self._encode_entropy()
            if token not in self._tokens:
                return token
        raise RuntimeError("entropy source produced repeated values")

    def _encode_entropy(self) -> str:
        entropy = self._random_bytes(32)
        if not isinstance(entropy, bytes) or len(entropy) != 32:
            raise ValueError("entropy source must return exactly 32 bytes")
        return base64.urlsafe_b64encode(entropy).decode().rstrip("=")
