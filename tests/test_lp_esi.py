import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from test_esi import settings

from app.esi import EsiClient, EsiDataError, EsiError

PID = "11111111-1111-4111-8111-111111111111"


def detail(**changes):
    return {
        "id": PID,
        "name": "LP对账-C海军",
        "state": "Active",
        "last_modified": "2026-10-18T00:00:00Z",
        "configuration": {
            "earn_loyalty_point": {"corporations": [{"corporation_id": 1000035}]}
        },
        "contribution": {"submission_multiplier": 1, "participation_limit": 1000},
        "progress": {"current": 100, "desired": 10000},
        **changes,
    }


def run(handler, operation):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await operation(EsiClient(settings(), http))

    return asyncio.run(scenario())


def test_project_cursor_pages_are_complete_and_state_all():
    def handle(request):
        assert request.url.host == "esi.evetech.net"
        assert request.url.params["state"] == "All"
        assert request.url.params["limit"] == "100"
        assert request.headers["X-Compatibility-Date"] == "2026-05-19"
        assert request.headers["Authorization"] == "Bearer private-token"
        assert "eve-income-tracker" in request.headers["User-Agent"]
        if request.url.params["after"] == "0":
            return httpx.Response(
                200, json={"projects": [detail()], "cursor": {"after": "next"}}
            )
        assert request.url.params["after"] == "next"
        return httpx.Response(200, json={"projects": []})

    result = run(handle, lambda c: c.fetch_corporation_projects("private-token", 9001))
    assert len(result) == 1 and result[0].project_id == PID


def test_project_detail_preserves_source_multiplier_limits_and_state():
    result = run(
        lambda r: httpx.Response(200, json=detail(state="Closed")),
        lambda c: c.fetch_project("token", 9001, PID),
    )
    assert result.source_corporation_ids == (1000035,)
    assert result.multiplier == Decimal(1)
    assert (
        result.state,
        result.participation_limit,
        result.current,
        result.desired,
    ) == ("Closed", 1000, 100, 10000)


def test_own_contribution_returns_integer_and_optional_modified_time():
    def handle(request):
        assert request.url.path == f"/corporations/9001/projects/{PID}/contribution/101"
        return httpx.Response(200, json={"contributed": 123})

    result = run(
        handle, lambda c: c.fetch_project_contribution("token", 9001, PID, 101)
    )
    assert result.contributed == 123 and result.last_modified is None
    result = run(
        lambda r: httpx.Response(
            200, json={"contributed": 123, "last_modified": "2026-10-18T00:00:00Z"}
        ),
        lambda c: c.fetch_project_contribution("token", 9001, PID, 101),
    )
    assert result.last_modified == datetime(2026, 10, 18, tzinfo=UTC)


@pytest.mark.parametrize("bad", [True, -1, 2**63, 1.5, "12", None])
def test_invalid_contribution_is_sanitized(bad):
    with pytest.raises(EsiDataError):
        run(
            lambda r: httpx.Response(200, json={"contributed": bad}),
            lambda c: c.fetch_project_contribution("private-token", 9001, PID, 101),
        )


@pytest.mark.parametrize("mode", ["repeated", "malformed"])
def test_repeated_cursor_and_malformed_later_page_fail_without_partial_list(mode):
    def handle(request):
        after = request.url.params["after"]
        if after != "0" and mode == "malformed":
            return httpx.Response(200, json={"projects": ["invalid"]})
        return httpx.Response(
            200, json={"projects": [detail()], "cursor": {"after": "next"}}
        )

    with pytest.raises(EsiDataError):
        run(handle, lambda c: c.fetch_corporation_projects("token", 9001))


@pytest.mark.parametrize("status", [401, 403, 404, 304])
def test_project_failures_do_not_return_zero_or_private_body(status):
    with pytest.raises(EsiError) as caught:
        run(
            lambda r: httpx.Response(status, text="private-body"),
            lambda c: c.fetch_project_contribution("private-token", 9001, PID, 101),
        )
    assert "private" not in str(caught.value)
    if status == 403:
        assert caught.value.code == "project_permission_denied"


def test_invalid_uuid_and_naive_time_are_rejected():
    with pytest.raises(EsiDataError):
        run(
            lambda r: pytest.fail("invalid ID sent to server"),
            lambda c: c.fetch_project("token", 9001, "../../elsewhere"),
        )
    with pytest.raises(EsiDataError):
        run(
            lambda r: httpx.Response(
                200, json={"contributed": 1, "last_modified": "2026-10-18T00:00:00"}
            ),
            lambda c: c.fetch_project_contribution("token", 9001, PID, 101),
        )


def test_public_character_profile_uses_no_authorization_header():
    def handle(request):
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"corporation_id": 9001})

    assert run(handle, lambda c: c.fetch_character_corporation(101)) == 9001


def test_detail_must_match_requested_project_id():
    with pytest.raises(EsiDataError):
        run(
            lambda r: httpx.Response(
                200, json=detail(id="22222222-2222-4222-8222-222222222222")
            ),
            lambda c: c.fetch_project("token", 9001, PID),
        )
