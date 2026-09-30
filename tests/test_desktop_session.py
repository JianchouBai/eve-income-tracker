import base64
from datetime import UTC, datetime, timedelta

import pytest

from app import desktop_session
from app.desktop_session import DesktopLaunchTokenStore, DesktopSessionError


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def decode_unpadded(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_launch_token_has_32_random_bytes_and_consumes_to_independent_session():
    store = DesktopLaunchTokenStore(clock=Clock())

    first = store.issue()
    second = store.issue()
    desktop_session_id = store.consume(first)

    assert len(decode_unpadded(first)) == 32
    assert len(decode_unpadded(second)) == 32
    assert len(decode_unpadded(desktop_session_id)) == 32
    assert first != second
    assert desktop_session_id not in {first, second}
    with pytest.raises(DesktopSessionError) as replay:
        store.consume(first)
    assert replay.value.code == "launch_token_unknown"


def test_launch_token_expires_at_exactly_sixty_seconds_and_cleanup_is_bounded():
    clock = Clock()
    store = DesktopLaunchTokenStore(clock=clock)
    expired = store.issue()
    clock.now += timedelta(seconds=59, microseconds=999999)
    current = store.issue()

    assert store.consume(expired)
    clock.now += timedelta(seconds=60)
    with pytest.raises(DesktopSessionError) as expired_error:
        store.consume(current)
    assert expired_error.value.code == "launch_token_expired"
    assert store.cleanup() == 0


def test_unknown_token_comparison_uses_constant_time_primitive(monkeypatch):
    store = DesktopLaunchTokenStore(clock=Clock())
    store.issue()
    calls = []
    real_compare = desktop_session.secrets.compare_digest

    def tracked_compare(actual, candidate):
        calls.append((actual, candidate))
        return real_compare(actual, candidate)

    monkeypatch.setattr(desktop_session.secrets, "compare_digest", tracked_compare)

    with pytest.raises(DesktopSessionError) as caught:
        store.consume("x" * 43)

    assert caught.value.code == "launch_token_unknown"
    assert calls
    assert all("x" * 43 in pair for pair in calls)


def test_launch_errors_do_not_echo_tokens():
    store = DesktopLaunchTokenStore(clock=Clock())
    token = store.issue()
    store.consume(token)

    with pytest.raises(DesktopSessionError) as caught:
        store.consume(token)

    rendered = f"{caught.value!s} {caught.value!r}"
    assert caught.value.code == "launch_token_unknown"
    assert token not in rendered
