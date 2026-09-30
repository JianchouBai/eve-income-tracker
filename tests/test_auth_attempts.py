import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.auth_attempts import AuthorizationAttemptError, AuthorizationAttemptStore


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def decode_unpadded(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_start_uses_independent_high_entropy_values_and_exact_ten_minute_expiry():
    clock = Clock()
    store = AuthorizationAttemptStore(clock=clock)

    first = store.start("owner-session-a")
    second = store.start("owner-session-a")

    assert len(decode_unpadded(first.attempt_id)) == 32
    assert len(decode_unpadded(first.state)) == 32
    assert len(decode_unpadded(first.verifier)) == 32
    assert first.attempt_id != second.attempt_id
    assert first.state != second.state
    assert first.verifier != second.verifier
    assert first.challenge == (
        base64.urlsafe_b64encode(hashlib.sha256(first.verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert first.expires_at == clock.now + timedelta(minutes=10)
    rendered = repr(first)
    for secret in (
        first.attempt_id,
        first.state,
        first.verifier,
        "owner-session-a",
    ):
        assert secret not in rendered


def test_pending_status_survives_until_exact_expiry_then_is_removed():
    clock = Clock()
    store = AuthorizationAttemptStore(clock=clock)
    attempt = store.start("owner")

    clock.now += timedelta(minutes=10) - timedelta(microseconds=1)
    assert store.consume_status(attempt.attempt_id, "owner").status == "pending"

    clock.now += timedelta(microseconds=1)
    with pytest.raises(AuthorizationAttemptError) as caught:
        store.consume_status(attempt.attempt_id, "owner")
    assert caught.value.code == "attempt_expired"
    with pytest.raises(AuthorizationAttemptError) as repeated:
        store.consume_status(attempt.attempt_id, "owner")
    assert repeated.value.code == "attempt_unknown"


def test_cleanup_removes_only_expired_attempts():
    clock = Clock()
    store = AuthorizationAttemptStore(clock=clock)
    expired = store.start("owner")
    clock.now += timedelta(minutes=5)
    current = store.start("owner")
    clock.now += timedelta(minutes=5)

    assert store.cleanup() == 1
    with pytest.raises(AuthorizationAttemptError) as caught:
        store.claim_callback(expired.state, None)
    assert caught.value.code == "attempt_unknown"
    assert store.consume_status(current.attempt_id, "owner").status == "pending"


def test_callback_without_cookie_claims_once_then_success_consumes_once():
    store = AuthorizationAttemptStore(clock=Clock())
    attempt = store.start("owner")
    claimed = store.claim_callback(attempt.state, None)

    assert claimed.attempt_id == attempt.attempt_id
    assert claimed.verifier == attempt.verifier
    assert attempt.verifier not in repr(claimed)
    with pytest.raises(AuthorizationAttemptError) as replay:
        store.claim_callback(attempt.state, None)
    assert replay.value.code == "callback_replayed"

    store.succeed(claimed.attempt_id)
    status = store.consume_status(attempt.attempt_id, "owner")
    assert status.status == "succeeded"
    assert status.error_code is None
    with pytest.raises(AuthorizationAttemptError) as consumed:
        store.consume_status(attempt.attempt_id, "owner")
    assert consumed.value.code == "attempt_unknown"


def test_present_different_callback_session_is_rejected_without_claiming():
    store = AuthorizationAttemptStore(clock=Clock())
    attempt = store.start("owner")

    with pytest.raises(AuthorizationAttemptError) as caught:
        store.claim_callback(attempt.state, "different-session")
    assert caught.value.code == "attempt_owner_mismatch"

    claimed = store.claim_callback(attempt.state, "owner")
    assert claimed.attempt_id == attempt.attempt_id


def test_unknown_expired_and_replayed_callback_states_are_distinctly_rejected():
    clock = Clock()
    store = AuthorizationAttemptStore(clock=clock)

    with pytest.raises(AuthorizationAttemptError) as unknown:
        store.claim_callback("unknown-state", None)
    assert unknown.value.code == "attempt_unknown"

    expired = store.start("owner")
    clock.now += timedelta(minutes=10)
    with pytest.raises(AuthorizationAttemptError) as expired_error:
        store.claim_callback(expired.state, None)
    assert expired_error.value.code == "attempt_expired"

    current = store.start("owner")
    store.claim_callback(current.state, None)
    with pytest.raises(AuthorizationAttemptError) as replay:
        store.claim_callback(current.state, "owner")
    assert replay.value.code == "callback_replayed"


def test_polling_always_requires_owner_and_terminal_failure_is_consumed_once():
    store = AuthorizationAttemptStore(clock=Clock())
    attempt = store.start("owner")
    assert store.consume_status(attempt.attempt_id, "owner").status == "pending"

    with pytest.raises(AuthorizationAttemptError) as wrong_owner:
        store.consume_status(attempt.attempt_id, "different")
    assert wrong_owner.value.code == "attempt_owner_mismatch"

    claimed = store.claim_callback(attempt.state, None)
    store.fail(claimed.attempt_id, "token_exchange_failed")

    with pytest.raises(AuthorizationAttemptError) as wrong_terminal_owner:
        store.consume_status(attempt.attempt_id, "different")
    assert wrong_terminal_owner.value.code == "attempt_owner_mismatch"

    status = store.consume_status(attempt.attempt_id, "owner")
    assert status.status == "failed"
    assert status.error_code == "token_exchange_failed"
    assert attempt.verifier not in repr(status)
    with pytest.raises(AuthorizationAttemptError) as consumed:
        store.consume_status(attempt.attempt_id, "owner")
    assert consumed.value.code == "attempt_unknown"


def test_attempt_errors_and_invalid_failure_codes_never_echo_secret_values():
    store = AuthorizationAttemptStore(clock=Clock())
    attempt = store.start("private-owner")
    claimed = store.claim_callback(attempt.state, None)

    with pytest.raises(ValueError):
        store.fail(claimed.attempt_id, "private provider error text")
    store.fail(claimed.attempt_id, "safe_error")

    with pytest.raises(AuthorizationAttemptError) as caught:
        store.consume_status(attempt.attempt_id, "private-other-owner")
    rendered = f"{caught.value!s} {caught.value!r}"
    assert caught.value.code == "attempt_owner_mismatch"
    for secret in (
        attempt.attempt_id,
        attempt.state,
        attempt.verifier,
        "private-owner",
        "private-other-owner",
    ):
        assert secret not in rendered
