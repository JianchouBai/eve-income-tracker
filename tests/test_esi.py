import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.esi import EsiAuthorizationError, EsiClient, EsiDataError, EsiError


def settings() -> Settings:
    return Settings(
        _env_file=None, eve_user_agent="eve-income-tracker/0.1 (admin@example.com)"
    )


def row(journal_id: int, amount: str) -> bytes:
    quote = chr(34)
    fields = [
        f"{quote}id{quote}: {journal_id}",
        f"{quote}date{quote}: {quote}2026-09-27T12:00:00Z{quote}",
        f"{quote}amount{quote}: {amount}",
        f"{quote}ref_type{quote}: {quote}bounty_prizes{quote}",
    ]
    return ("[{" + ", ".join(fields) + "}]").encode()


def test_fetches_all_pages_with_exact_decimals_and_required_headers():
    async def scenario():
        requests = []

        def handle(request: httpx.Request):
            requests.append(request)
            assert str(request.url).startswith(
                "https://esi.evetech.net/latest/characters/101/wallet/journal/"
            )
            assert request.headers["X-Compatibility-Date"] == "2026-05-19"
            assert (
                request.headers["User-Agent"]
                == "eve-income-tracker/0.1 (admin@example.com)"
            )
            assert request.headers["Authorization"] == "Bearer access-token"
            page = request.url.params.get("page")
            return httpx.Response(
                200,
                headers={"X-Pages": "2"},
                content=row(
                    1 if page == "1" else 2,
                    "9007199254740993.01" if page == "1" else "1e-2",
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            entries = await EsiClient(settings(), http).fetch_wallet_journal(
                "access-token", 101
            )

        assert [entry.amount for entry in entries] == [
            Decimal("9007199254740993.01"),
            Decimal("0.01"),
        ]
        assert entries[0].date_utc == datetime(2026, 9, 27, 12, tzinfo=UTC)
        assert [request.url.params.get("page") for request in requests] == ["1", "2"]

    asyncio.run(scenario())


@pytest.mark.parametrize("pages", [None, "many", "0", "101"])
def test_pages_default_or_invalid_values(pages: str | None):
    async def scenario():
        headers = {} if pages is None else {"X-Pages": pages}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers=headers, content=row(1, "1.00")
                )
            )
        ) as http:
            client = EsiClient(settings(), http)
            if pages is None:
                assert len(await client.fetch_wallet_journal("token", 101)) == 1
            else:
                with pytest.raises(EsiDataError) as caught:
                    await client.fetch_wallet_journal("token", 101)
                assert caught.value.code == "invalid_pages"

    asyncio.run(scenario())


def test_retry_after_is_capped_and_invalid_values_use_backoff():
    async def scenario(retry_after: str, expected: list[float]):
        attempts = 0
        sleeps = []

        async def sleeper(delay: float):
            sleeps.append(delay)

        def handle(request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429, headers={"Retry-After": retry_after})
            return httpx.Response(200, content=row(1, "1.00"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            entries = await EsiClient(
                settings(), http, sleeper=sleeper, jitter=lambda: 0
            ).fetch_wallet_journal("token", 101)
        assert len(entries) == 1
        assert sleeps == expected

    asyncio.run(scenario("120", [60, 60]))
    asyncio.run(scenario("invalid", [1, 2]))


def test_network_and_server_failures_stop_after_three_attempts():
    async def scenario(failure: str):
        attempts = 0

        async def sleeper(delay: float):
            return None

        def handle(request):
            nonlocal attempts
            attempts += 1
            if failure == "network":
                raise httpx.ReadTimeout("secret token", request=request)
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(EsiError) as caught:
                await EsiClient(
                    settings(), http, sleeper=sleeper, jitter=lambda: 0
                ).fetch_wallet_journal("token", 101)
        assert caught.value.code == "request_failed"
        assert attempts == 3
        assert "secret token" not in str(caught.value)

    asyncio.run(scenario("network"))
    asyncio.run(scenario("server"))


def test_authorization_failure_is_not_retried():
    async def scenario():
        attempts = 0

        def handle(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(EsiAuthorizationError):
                await EsiClient(settings(), http).fetch_wallet_journal("token", 101)
        assert attempts == 1

    asyncio.run(scenario())


def test_malformed_later_page_raises_sanitized_data_error_without_partial_result():
    async def scenario():
        def handle(request):
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200, headers={"X-Pages": "2"}, content=row(1, "1.00")
                )
            quote = chr(34)
            content = f"[{{{quote}id{quote}: 2, {quote}date{quote}: 7, {quote}amount{quote}: 1.00, {quote}ref_type{quote}: {quote}bounty_prizes{quote}}}]".encode()
            return httpx.Response(200, content=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(EsiDataError) as caught:
                await EsiClient(settings(), http).fetch_wallet_journal("token", 101)
        assert caught.value.code == "invalid_data"

    asyncio.run(scenario())


def test_naive_timestamp_is_rejected_as_invalid_esi_data():
    async def scenario():
        payload = json.dumps(
            [
                {
                    "id": 1,
                    "date": "2026-09-27T12:00:00",
                    "amount": 1.0,
                    "ref_type": "bounty_prizes",
                }
            ]
        ).encode()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=payload)
            )
        ) as http:
            with pytest.raises(EsiDataError):
                await EsiClient(settings(), http).fetch_wallet_journal("token", 101)

    asyncio.run(scenario())


def test_integer_monetary_fields_are_normalized_to_decimal():
    async def scenario():
        payload = json.dumps(
            [
                {
                    "id": 1,
                    "date": "2026-09-27T12:00:00Z",
                    "amount": 7,
                    "tax": 2,
                    "balance": 3,
                    "ref_type": "bounty_prizes",
                }
            ]
        ).encode()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=payload)
            )
        ) as http:
            entry = (
                await EsiClient(settings(), http).fetch_wallet_journal("token", 101)
            )[0]
        assert (entry.amount, entry.tax, entry.balance) == (
            Decimal(7),
            Decimal(2),
            Decimal(3),
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "user_agent",
    ["", "eve-income-tracker", "eve-income-tracker/0.1 (your-email@example.com)"],
)
def test_invalid_user_agent_is_refused_before_http_request(user_agent: str):
    async def scenario():
        requests = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: requests.append(request))
        ) as http:
            configured = Settings(_env_file=None, eve_user_agent=user_agent)
            with pytest.raises(EsiError) as caught:
                await EsiClient(configured, http).fetch_wallet_journal("token", 101)
        assert caught.value.code == "invalid_configuration"
        assert requests == []

    asyncio.run(scenario())
