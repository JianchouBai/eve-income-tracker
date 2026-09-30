from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.sso import PkcePair

ATTEMPT_LIFETIME = timedelta(minutes=10)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuthorizationAttemptError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(frozen=True)
class AuthorizationAttempt:
    attempt_id: str = field(repr=False)
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    challenge: str
    expires_at: datetime


@dataclass(frozen=True)
class ClaimedAuthorizationAttempt:
    attempt_id: str = field(repr=False)
    verifier: str = field(repr=False)


@dataclass(frozen=True)
class AuthorizationAttemptStatus:
    status: Literal["pending", "succeeded", "failed"]
    error_code: str | None = None


@dataclass(repr=False)
class _StoredAttempt:
    attempt_id: str
    state: str
    verifier: str
    challenge: str
    owner_session_id: str
    expires_at: datetime
    phase: Literal["pending", "claimed", "succeeded", "failed"] = "pending"
    error_code: str | None = None


class AuthorizationAttemptStore:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._clock = clock
        self._random_bytes = random_bytes
        self._by_id: dict[str, _StoredAttempt] = {}
        self._by_state: dict[str, _StoredAttempt] = {}

    def start(self, owner_session_id: str) -> AuthorizationAttempt:
        self._validate_owner(owner_session_id)
        self.cleanup()
        attempt_id = self._new_token(self._by_id)
        state = self._new_token(self._by_state)
        pair = PkcePair.generate(self._random_bytes)
        expires_at = self._clock() + ATTEMPT_LIFETIME
        stored = _StoredAttempt(
            attempt_id=attempt_id,
            state=state,
            verifier=pair.verifier,
            challenge=pair.challenge,
            owner_session_id=owner_session_id,
            expires_at=expires_at,
        )
        self._by_id[attempt_id] = stored
        self._by_state[state] = stored
        return AuthorizationAttempt(
            attempt_id,
            state,
            pair.verifier,
            pair.challenge,
            expires_at,
        )

    def claim_callback(
        self,
        state: str,
        session_id: str | None,
    ) -> ClaimedAuthorizationAttempt:
        attempt = self._by_state.get(state)
        if attempt is None:
            self.cleanup()
            raise AuthorizationAttemptError("attempt_unknown")
        self._ensure_current(attempt)
        self._cleanup_except(attempt)
        if attempt.phase != "pending":
            raise AuthorizationAttemptError("callback_replayed")
        if session_id is not None and not secrets.compare_digest(
            session_id,
            attempt.owner_session_id,
        ):
            raise AuthorizationAttemptError("attempt_owner_mismatch")
        attempt.phase = "claimed"
        return ClaimedAuthorizationAttempt(attempt.attempt_id, attempt.verifier)

    def succeed(self, attempt_id: str) -> None:
        attempt = self._get_current(attempt_id)
        if attempt.phase != "claimed":
            raise AuthorizationAttemptError("attempt_not_claimed")
        attempt.phase = "succeeded"

    def fail(self, attempt_id: str, error_code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise ValueError("error code must be a safe identifier")
        attempt = self._get_current(attempt_id)
        if attempt.phase != "claimed":
            raise AuthorizationAttemptError("attempt_not_claimed")
        attempt.phase = "failed"
        attempt.error_code = error_code

    def consume_status(
        self,
        attempt_id: str,
        owner_session_id: str,
    ) -> AuthorizationAttemptStatus:
        self._validate_owner(owner_session_id)
        attempt = self._get_current(attempt_id)
        if not secrets.compare_digest(owner_session_id, attempt.owner_session_id):
            raise AuthorizationAttemptError("attempt_owner_mismatch")
        if attempt.phase in {"pending", "claimed"}:
            return AuthorizationAttemptStatus("pending")
        status = AuthorizationAttemptStatus(attempt.phase, attempt.error_code)
        self._remove(attempt)
        return status

    def discard(self, attempt_id: str) -> None:
        attempt = self._by_id.get(attempt_id)
        if attempt is not None:
            self._remove(attempt)

    def cleanup(self) -> int:
        now = self._clock()
        expired = [
            attempt for attempt in self._by_id.values() if now >= attempt.expires_at
        ]
        for attempt in expired:
            self._remove(attempt)
        return len(expired)

    def _new_token(self, existing: dict[str, _StoredAttempt]) -> str:
        for _ in range(16):
            entropy = self._random_bytes(32)
            if not isinstance(entropy, bytes) or len(entropy) != 32:
                raise ValueError("entropy source must return exactly 32 bytes")
            token = base64.urlsafe_b64encode(entropy).decode().rstrip("=")
            if token not in existing:
                return token
        raise RuntimeError("entropy source produced repeated values")

    def _get_current(self, attempt_id: str) -> _StoredAttempt:
        attempt = self._by_id.get(attempt_id)
        if attempt is None:
            self.cleanup()
            raise AuthorizationAttemptError("attempt_unknown")
        self._ensure_current(attempt)
        self._cleanup_except(attempt)
        return attempt

    def _ensure_current(self, attempt: _StoredAttempt) -> None:
        if self._clock() >= attempt.expires_at:
            self._remove(attempt)
            raise AuthorizationAttemptError("attempt_expired")

    def _cleanup_except(self, retained: _StoredAttempt) -> None:
        now = self._clock()
        for attempt in tuple(self._by_id.values()):
            if attempt is not retained and now >= attempt.expires_at:
                self._remove(attempt)

    def _remove(self, attempt: _StoredAttempt) -> None:
        self._by_id.pop(attempt.attempt_id, None)
        self._by_state.pop(attempt.state, None)

    @staticmethod
    def _validate_owner(owner_session_id: str) -> None:
        if (
            not isinstance(owner_session_id, str)
            or not owner_session_id
            or len(owner_session_id) > 512
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in owner_session_id
            )
        ):
            raise ValueError("owner session identifier is invalid")
