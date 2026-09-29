import base64
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.config import Settings
from app.esi import EsiDataError, EsiError
from app.models import JournalEntry
from app.security import TokenCipher
from app.sso import CharacterIdentity, SsoError, TokenSet
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    Database,
    JournalRepository,
)
from app.sync import SyncAllResult, SyncResult
from app.web import AppServices, create_app


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 9, 28, 16, 30, tzinfo=UTC)

    def now_utc(self):
        return self.current


class FakeSso:
    def __init__(self, clock):
        self.clock = clock
        self.identity = CharacterIdentity(
            101, "Verified Pilot", ("esi-wallet.read_character_wallet.v1",)
        )
        self.refresh_token = "private-refresh"
        self.exchange_calls = []
        self.verified_tokens = []
        self.failure = None

    async def build_authorization_url(self, state):
        if self.failure == "login":
            raise RuntimeError("private-sso-secret")
        return "https://login.eveonline.com/v2/oauth/authorize?state=" + state

    async def exchange_code(self, code):
        self.exchange_calls.append(code)
        if self.failure == "exchange":
            raise SsoError("request_failed", "private-sso-secret")
        return TokenSet(
            "private-access",
            self.refresh_token,
            self.clock.now_utc() + timedelta(minutes=20),
        )

    async def verify_access_token(self, token):
        self.verified_tokens.append(token)
        if self.failure == "verify":
            raise SsoError("unsupported_es256", "private-sso-secret")
        if isinstance(self.failure, str) and self.failure.startswith("verify:"):
            raise SsoError(self.failure.removeprefix("verify:"), "private-sso-secret")
        return self.identity


class FakeSync:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail = False
        self.results = ()

    async def sync_character(self, character_id, initial_access_token=None):
        self.calls.append((character_id, initial_access_token))
        self.clock.current += timedelta(minutes=1)
        if self.fail:
            raise RuntimeError("private-sync-token")
        return SyncResult(character_id, 0, self.clock.now_utc(), "ok", None)

    async def sync_group(self, group_id):
        self.calls.append(("group", group_id))
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail:
            raise RuntimeError("private-sync-token")
        return SyncAllResult(self.results)


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        _env_file=None,
        eve_client_id="client-id",
        eve_client_secret="private-client-secret",
        app_session_secret="session-secret-" * 4,
        app_encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "wallet.sqlite3",
        eve_user_agent="eve-income-tracker/0.1 (admin@example.com)",
    )
    database = Database(settings.database_path)
    clock = FakeClock()
    services = AppServices(
        CharacterRepository(database),
        CharacterGroupRepository(database),
        JournalRepository(database),
        TokenCipher(settings.app_encryption_key),
        FakeSso(clock),
        FakeSync(clock),
    )
    return settings, services, clock


def session_data(client, secret):
    signed = client.cookies.get("session")
    return json.loads(base64.b64decode(TimestampSigner(secret).unsign(signed)))


def test_home_and_health_initialize_storage_without_working_directory_dependency(
    context, tmp_path, monkeypatch
):
    settings, services, clock = context
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(settings, services, clock)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        home = client.get("/")
        assert home.status_code == 200
        assert "/auth/login" in home.text
        assert services.characters.list_all() == []


def test_invalid_configuration_home_names_only_missing_fields_and_disables_login(
    context,
):
    settings, services, clock = context
    settings.eve_client_id = ""
    settings.app_session_secret = "private-short-secret"
    app = create_app(settings, services, clock)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "EVE_CLIENT_ID" in response.text
        assert "APP_SESSION_SECRET" in response.text
        assert "/auth/login" not in response.text
        assert "private-" not in response.text
        rejected = client.get("/auth/login", follow_redirects=False)
        assert rejected.status_code == 503
        assert rejected.json()["error"]["code"] == "configuration_required"


def test_login_redirect_contains_high_entropy_state_in_minimal_session(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        response = client.get("/auth/login", follow_redirects=False)
        assert response.status_code == 302
        session = session_data(client, settings.app_session_secret)
        assert set(session) == {"oauth_state", "csrf_token"}
        assert len(session["oauth_state"]) >= 43
        assert len(session["csrf_token"]) >= 43
        assert (
            response.headers["location"]
            == "https://login.eveonline.com/v2/oauth/authorize?state="
            + session["oauth_state"]
        )
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert "secure" not in cookie
        second = client.get("/auth/login", follow_redirects=False)
        assert second.headers["location"] != response.headers["location"]
        assert "private-" not in repr(session)


@pytest.mark.parametrize(
    "callback",
    [
        "https://localhost/auth/callback",
        "http://example.com/auth/callback",
        "https://example.com/auth/callback",
    ],
)
def test_non_local_http_callback_enables_secure_cookie(context, callback):
    settings, services, clock = context
    settings.eve_callback_url = callback
    with TestClient(create_app(settings, services, clock)) as client:
        response = client.get("/auth/login", follow_redirects=False)
        assert "secure" in response.headers["set-cookie"].lower()


def test_main_invalid_config_builds_storage_without_crypto_or_network_services(context):
    from app.main import build_app

    settings, _, _ = context
    settings.app_encryption_key = "private-invalid-key"
    settings.app_session_secret = ""
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.services.characters.list_all() == []
        assert app.state.services.cipher is None
        assert app.state.services.sso is None
        assert app.state.services.sync is None
        assert "APP_ENCRYPTION_KEY" in client.get("/").text


def authorize(client, settings, **extra):
    client.get("/auth/login", follow_redirects=False)
    state = session_data(client, settings.app_session_secret)["oauth_state"]
    return client.get(
        "/auth/callback",
        params={"code": "private-code", "state": state, **extra},
        follow_redirects=False,
    )


def csrf_headers(client, settings):
    return {
        "X-CSRF-Token": session_data(client, settings.app_session_secret)["csrf_token"]
    }


@pytest.mark.parametrize("state", [None, "", "wrong", "非匹配状态"])
def test_invalid_callback_state_cannot_exchange_or_persist(context, state):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        client.get("/auth/login", follow_redirects=False)
        params = {"code": "private-code"}
        if state is not None:
            params["state"] = state
        response = client.get("/auth/callback", params=params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_state"
        assert services.sso.exchange_calls == []
        assert services.characters.list_all() == []


def test_callback_uses_verified_identity_encrypts_token_and_captures_start_before_sync(
    context,
):
    settings, services, clock = context
    started = clock.now_utc()
    with TestClient(create_app(settings, services, clock)) as client:
        response = authorize(client, settings, character_id=999, name="Attacker")
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        (record,) = services.characters.list_all()
        assert (record.character_id, record.name) == (101, "Verified Pilot")
        assert record.tracking_started_at == started
        assert record.tracking_started_at < clock.now_utc()
        assert record.encrypted_refresh_token != "private-refresh"
        assert (
            services.cipher.decrypt(record.encrypted_refresh_token) == "private-refresh"
        )
        assert services.sso.verified_tokens == ["private-access"]
        assert services.sync.calls == [(101, "private-access")]
        session = session_data(client, settings.app_session_secret)
        assert set(session) == {"viewer_authenticated", "csrf_token"}
        assert session["viewer_authenticated"] is True
        assert "private-" not in repr(session)


def test_adding_character_and_reauthorizing_preserves_original_tracking_start(context):
    settings, services, clock = context
    started = clock.now_utc()
    with TestClient(create_app(settings, services, clock)) as client:
        assert authorize(client, settings).status_code == 302
        group = services.groups.create("Reauthorization Group", clock.now_utc())
        services.characters.set_group(101, group.group_id)
        services.sso.identity = CharacterIdentity(
            202, "Second Pilot", ("esi-wallet.read_character_wallet.v1",)
        )
        assert authorize(client, settings).status_code == 302
        services.sso.identity = CharacterIdentity(
            101, "Renamed Pilot", ("esi-wallet.read_character_wallet.v1",)
        )
        services.sso.refresh_token = "private-rotated-refresh"
        assert authorize(client, settings).status_code == 302
        first, second = services.characters.list_all()
        assert first.character_id == 101 and second.character_id == 202
        assert first.name == "Renamed Pilot"
        assert first.group_id == group.group_id
        assert first.tracking_started_at == started
        assert (
            services.cipher.decrypt(first.encrypted_refresh_token)
            == "private-rotated-refresh"
        )


def test_callback_state_cannot_be_replayed_even_with_original_signed_cookie(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        client.get("/auth/login", follow_redirects=False)
        cookie = client.cookies.get("session")
        state = session_data(client, settings.app_session_secret)["oauth_state"]
        params = {"code": "private-code", "state": state}
        assert (
            client.get(
                "/auth/callback", params=params, follow_redirects=False
            ).status_code
            == 302
        )
        client.cookies.clear()
        client.cookies.set("session", cookie)
        replay = client.get("/auth/callback", params=params, follow_redirects=False)
        assert replay.status_code == 400
        assert services.sso.exchange_calls == ["private-code"]


@pytest.mark.parametrize("failure", ["exchange", "verify"])
def test_callback_failures_are_sanitized_and_do_not_authorize(context, failure):
    settings, services, clock = context
    services.sso.failure = failure
    with TestClient(create_app(settings, services, clock)) as client:
        response = authorize(client, settings)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "authorization_failed"
        assert "private-" not in response.text
        assert services.characters.list_all() == []
        assert not session_data(client, settings.app_session_secret).get(
            "viewer_authenticated"
        )


@pytest.mark.parametrize(
    ("failure", "stage", "code"),
    [
        ("exchange", "token_exchange", "request_failed"),
        ("verify", "token_verification", "unsupported_es256"),
    ],
)
def test_callback_failure_logs_safe_stage_and_code(
    context, caplog, failure, stage, code
):
    settings, services, clock = context
    services.sso.failure = failure
    with (
        caplog.at_level(logging.WARNING, logger="app.web"),
        TestClient(create_app(settings, services, clock)) as client,
    ):
        response = authorize(client, settings)

    assert response.status_code == 502
    assert f"EVE authorization failed: stage={stage} code={code}" in caplog.messages
    assert "private-" not in caplog.text


@pytest.mark.parametrize(
    "code",
    [
        "expired_token",
        "immature_token",
        "invalid_audience_claim",
        "invalid_issuer",
        "invalid_name_claim",
        "invalid_scope_format",
        "invalid_standard_claims",
        "invalid_subject_claim",
        "missing_required_claim",
        "wallet_scope_not_granted",
    ],
)
def test_callback_logs_new_claim_category_without_sensitive_values(
    context, caplog, code
):
    settings, services, clock = context
    services.sso.failure = f"verify:{code}"
    with (
        caplog.at_level(logging.WARNING, logger="app.web"),
        TestClient(create_app(settings, services, clock)) as client,
    ):
        response = authorize(client, settings)

    assert response.status_code == 502
    assert (
        f"EVE authorization failed: stage=token_verification code={code}"
        in caplog.messages
    )
    assert "private-" not in caplog.text


def test_callback_sync_failure_preserves_character_and_redirects_with_safe_status(
    context,
):
    settings, services, clock = context
    services.sync.fail = True
    started = clock.now_utc()
    with TestClient(create_app(settings, services, clock)) as client:
        response = authorize(client, settings)
        assert response.status_code == 302
        assert response.headers["location"] == "/?status=sync_failed"
        (record,) = services.characters.list_all()
        assert record.tracking_started_at == started
        assert (
            services.cipher.decrypt(record.encrypted_refresh_token) == "private-refresh"
        )


def test_logout_requires_csrf_and_keeps_persisted_characters_and_entries(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        entry = JournalEntry(1, 101, clock.now_utc(), Decimal(5), "bounty_prizes")
        services.journal.commit_sync_batch(101, [entry], clock.now_utc())
        before = services.characters.list_all()
        for token in [None, "wrong"]:
            response = client.post(
                "/auth/logout", headers={} if token is None else {"X-CSRF-Token": token}
            )
            assert response.status_code == 403
        csrf = session_data(client, settings.app_session_secret)["csrf_token"]
        response = client.post(
            "/auth/logout", headers={"X-CSRF-Token": csrf}, follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        assert client.cookies.get("session") is None
        assert services.characters.list_all() == before
        assert services.journal.list_for_report(
            {101},
            clock.now_utc() - timedelta(days=1),
            clock.now_utc() + timedelta(days=1),
        ) == [entry]


def seed_reports(services, clock):
    services.characters.upsert_authorization(
        202,
        "Disabled Pilot",
        services.cipher.encrypt("private-second-refresh"),
        clock.now_utc(),
    )
    services.characters.set_enabled(202, False)
    group = services.groups.create("Main Group", clock.now_utc())
    services.characters.set_group(101, group.group_id)
    services.characters.set_group(202, group.group_id)
    services.journal.commit_sync_batch(
        101,
        [
            JournalEntry(
                1,
                101,
                datetime(2026, 9, 27, 16, tzinfo=UTC),
                Decimal("10.100"),
                "bounty_prizes",
            ),
            JournalEntry(
                2,
                101,
                datetime(2026, 9, 29, 15, 59, tzinfo=UTC),
                Decimal("-2.20"),
                "npc_bounty_security_tax",
            ),
            JournalEntry(
                3,
                101,
                datetime(2026, 9, 29, 16, tzinfo=UTC),
                Decimal(1000),
                "bounty_prizes",
            ),
            JournalEntry(
                4,
                101,
                datetime(2026, 9, 28, 16, tzinfo=UTC),
                Decimal(999),
                "unknown_type",
            ),
            JournalEntry(
                5,
                101,
                datetime(2026, 9, 28, 16, tzinfo=UTC),
                Decimal(-500),
                "bounty_prizes",
            ),
        ],
        clock.now_utc(),
    )
    services.journal.commit_sync_batch(
        202,
        [
            JournalEntry(
                1,
                202,
                datetime(2026, 9, 28, 16, tzinfo=UTC),
                Decimal("20.020"),
                "ess_escrow_transfer",
            ),
        ],
        clock.now_utc(),
    )
    return group.group_id


@pytest.mark.parametrize(
    "route", ["/api/characters", "/api/groups", "/api/dashboard", "/api/entries"]
)
def test_read_api_requires_viewer_authentication(context, route):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        response = client.get(route)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"


def test_group_crud_membership_counts_and_delete_preserve_data(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        services.journal.commit_sync_batch(
            101,
            [
                JournalEntry(
                    77,
                    101,
                    clock.now_utc(),
                    Decimal("5.25"),
                    "bounty_prizes",
                    description="private-wallet-description",
                )
            ],
            clock.now_utc(),
        )
        headers = csrf_headers(client, settings)

        assert client.get("/api/groups").json() == {
            "groups": [],
            "ungrouped_count": 1,
        }
        first_response = client.post(
            "/api/groups", json={"name": " Alpha "}, headers=headers
        )
        assert first_response.status_code == 201
        first = first_response.json()["group"]
        assert first["name"] == "Alpha"
        assert first["member_count"] == 0
        assert datetime.fromisoformat(first["created_at"]).tzinfo is not None
        second = client.post(
            "/api/groups", json={"name": "Second"}, headers=headers
        ).json()["group"]
        duplicate = client.post("/api/groups", json={"name": "alpha"}, headers=headers)
        assert duplicate.status_code == 400
        assert duplicate.json()["error"]["code"] == "group_name_conflict"

        listed = client.get("/api/groups").json()
        assert [group["group_id"] for group in listed["groups"]] == [
            first["group_id"],
            second["group_id"],
        ]
        assert [group["member_count"] for group in listed["groups"]] == [0, 0]
        assert listed["ungrouped_count"] == 1

        assigned = client.post(
            "/api/characters/101/group",
            json={"group_id": first["group_id"]},
            headers=headers,
        )
        assert assigned.status_code == 200
        assert assigned.json()["character"]["group_id"] == first["group_id"]
        moved = client.post(
            "/api/characters/101/group",
            json={"group_id": second["group_id"]},
            headers=headers,
        )
        assert moved.json()["character"]["group_id"] == second["group_id"]
        assert [
            group["member_count"]
            for group in client.get("/api/groups").json()["groups"]
        ] == [0, 1]

        renamed = client.patch(
            f"/api/groups/{second['group_id']}",
            json={"name": "  PVE 主队  "},
            headers=headers,
        )
        assert renamed.status_code == 200
        assert renamed.json()["group"]["name"] == "PVE 主队"
        deleted = client.delete(f"/api/groups/{second['group_id']}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["moved_character_ids"] == [101]

        (character,) = services.characters.list_all()
        assert character.group_id is None
        assert character.encrypted_refresh_token != "private-refresh"
        assert (
            services.journal.list_for_report(
                {101},
                clock.now_utc() - timedelta(days=1),
                clock.now_utc() + timedelta(days=1),
            )[0].description
            == "private-wallet-description"
        )
        assert "private-wallet-description" not in deleted.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 51},
        {"name": 123},
        {"name": "Valid", "extra": "private-extra"},
    ],
)
def test_group_name_payload_validation_is_safe(context, payload):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        response = client.post(
            "/api/groups", json=payload, headers=csrf_headers(client, settings)
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert "private-extra" not in response.text


def test_group_mutations_require_authentication_and_csrf(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        group = services.groups.create("Alpha", clock.now_utc())
        services.characters.upsert_authorization(
            101, "Pilot", "private-token", clock.now_utc()
        )
        mutations = [
            ("POST", "/api/groups", {"name": "Second"}),
            ("PATCH", f"/api/groups/{group.group_id}", {"name": "Renamed"}),
            ("DELETE", f"/api/groups/{group.group_id}", None),
            ("POST", "/api/characters/101/group", {"group_id": group.group_id}),
        ]
        for method, route, payload in mutations:
            assert client.request(method, route, json=payload).status_code == 401
        authorize(client, settings)
        for method, route, payload in mutations:
            response = client.request(method, route, json=payload)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_failed"


def test_group_unknown_ids_return_safe_errors(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        headers = csrf_headers(client, settings)
        services.characters.upsert_authorization(
            101, "Pilot", "private-character-token", clock.now_utc()
        )
        cases = [
            client.patch("/api/groups/999", json={"name": "Missing"}, headers=headers),
            client.delete("/api/groups/999", headers=headers),
            client.post(
                "/api/characters/101/group", json={"group_id": 999}, headers=headers
            ),
            client.post(
                "/api/characters/999/group", json={"group_id": None}, headers=headers
            ),
        ]
        assert [response.status_code for response in cases] == [404, 404, 404, 404]
        assert [response.json()["error"]["code"] for response in cases] == [
            "unknown_group",
            "unknown_group",
            "unknown_group",
            "unknown_character",
        ]
        assert all("private-" not in response.text for response in cases)
        assert all("sqlite" not in response.text.lower() for response in cases)


def test_character_api_exposes_only_public_fields(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        seed_reports(services, clock)
        services.characters.mark_authorization_required(202)
        response = client.get("/api/characters")
        assert response.status_code == 200
        rows = response.json()["characters"]
        assert [row["character_id"] for row in rows] == [101, 202]
        assert set(rows[0]) == {
            "character_id",
            "name",
            "enabled",
            "authorization_required",
            "tracking_started_at",
            "last_synced_at",
            "group_id",
        }
        assert rows[1]["authorization_required"] is True
        assert "private-" not in response.text
        assert "encrypted_refresh_token" not in response.text


def test_dashboard_defaults_to_latest_thirty_beijing_days_and_enabled_roles(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        response = client.get(f"/api/dashboard?group_id={group_id}")
        assert response.status_code == 200
        report = response.json()
        assert (report["group_id"], report["group_name"]) == (group_id, "Main Group")
        assert (report["from"], report["to"]) == ("2026-08-31", "2026-09-29")
        assert report["selected_ids"] == [101]
        assert (report["income"], report["tax"], report["net"]) == (
            "10.1",
            "2.2",
            "7.9",
        )
        assert len(report["daily"]) == 30
        assert report["daily"][0] == {
            "date": "2026-08-31",
            "income": "0",
            "tax": "0",
            "net": "0",
        }
        assert report["daily"][-2]["income"] == "10.1"
        assert report["daily"][-1]["tax"] == "2.2"


def test_repeated_character_selection_includes_disabled_and_continuous_inclusive_dates(
    context,
):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        report = client.get(
            f"/api/dashboard?group_id={group_id}&from=2026-09-28&to=2026-09-29"
            "&character_id=101&character_id=202"
        ).json()
        assert report["selected_ids"] == [101, 202]
        assert (report["income"], report["tax"], report["net"]) == (
            "30.12",
            "2.2",
            "27.92",
        )
        assert len(report["daily"]) == 2
        assert [row["income"] for row in report["characters"]] == ["10.1", "20.02"]
        disabled_only = client.get(
            f"/api/dashboard?group_id={group_id}&from=2026-09-28&to=2026-09-29"
            "&character_id=202"
        ).json()
        assert disabled_only["income"] == "20.02"
        assert disabled_only["tax"] == "0"


def test_entries_filter_by_character_date_and_income_or_tax_classification(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        response = client.get(
            f"/api/entries?group_id={group_id}&from=2026-09-28&to=2026-09-29"
        )
        assert response.status_code == 200
        entries = response.json()["entries"]
        assert [entry["journal_id"] for entry in entries] == [1, 2]
        assert [entry["amount"] for entry in entries] == ["10.1", "-2.2"]
        assert [entry["kind"] for entry in entries] == ["income", "tax"]
        assert [entry["date"] for entry in entries] == ["2026-09-28", "2026-09-29"]
        assert all(entry["character_id"] == 101 for entry in entries)
        selected = client.get(
            f"/api/entries?group_id={group_id}&from=2026-09-28&to=2026-09-29"
            "&character_id=202"
        ).json()["entries"]
        assert len(selected) == 1 and selected[0]["character_id"] == 202


def test_group_report_requires_known_group_and_rejects_cross_group_selection(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        other = services.groups.create("Other Group", clock.now_utc())
        services.characters.upsert_authorization(
            303, "Other Pilot", "private-other-token", clock.now_utc()
        )
        services.characters.set_group(303, other.group_id)

        missing = client.get("/api/dashboard")
        unknown = client.get("/api/entries?group_id=999")
        cross_group = client.get(f"/api/dashboard?group_id={group_id}&character_id=303")
        services.groups.delete(group_id)
        deleted = client.get(f"/api/dashboard?group_id={group_id}")

        assert (missing.status_code, missing.json()["error"]["code"]) == (
            400,
            "invalid_request",
        )
        assert (unknown.status_code, unknown.json()["error"]["code"]) == (
            404,
            "unknown_group",
        )
        assert (
            cross_group.status_code,
            cross_group.json()["error"]["code"],
        ) == (400, "character_not_in_group")
        assert (deleted.status_code, deleted.json()["error"]["code"]) == (
            404,
            "unknown_group",
        )
        for response in (missing, unknown, cross_group, deleted):
            assert "private-" not in response.text
            assert "sqlite" not in response.text.lower()


def test_group_report_combines_external_donation_and_excludes_internal_transfer(
    context,
):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        services.journal.commit_sync_batch(
            101,
            [
                JournalEntry(
                    journal_id=6,
                    character_id=101,
                    date_utc=datetime(2026, 9, 28, 10, tzinfo=UTC),
                    amount=Decimal(25),
                    ref_type="player_donation",
                    description="external donation",
                    first_party_id=9001,
                    second_party_id=101,
                ),
                JournalEntry(
                    journal_id=7,
                    character_id=101,
                    date_utc=datetime(2026, 9, 28, 11, tzinfo=UTC),
                    amount=Decimal(40),
                    ref_type="player_donation",
                    description="internal donation",
                    first_party_id=202,
                    second_party_id=101,
                ),
            ],
            clock.now_utc(),
        )

        report = client.get(
            f"/api/dashboard?group_id={group_id}&from=2026-09-28&to=2026-09-29"
        ).json()
        assert (report["group_id"], report["group_name"]) == (group_id, "Main Group")
        assert report["selected_ids"] == [101]
        assert (report["income"], report["tax"], report["net"]) == (
            "35.1",
            "2.2",
            "32.9",
        )
        category = next(
            item for item in report["categories"] if item["category"] == "玩家转账"
        )
        assert category == {
            "category": "玩家转账",
            "income": "25",
            "tax": "0",
            "net": "25",
        }
        entries_payload = client.get(
            f"/api/entries?group_id={group_id}&from=2026-09-28&to=2026-09-29"
        ).json()
        assert (
            entries_payload["group_id"],
            entries_payload["group_name"],
        ) == (group_id, "Main Group")
        donation_rows = [
            entry
            for entry in entries_payload["entries"]
            if entry["ref_type"] == "player_donation"
        ]
        assert [(row["journal_id"], row["category"]) for row in donation_rows] == [
            (6, "玩家转账")
        ]

        disabled = client.get(
            f"/api/dashboard?group_id={group_id}&from=2026-09-28&to=2026-09-29"
            "&character_id=202"
        ).json()
        assert disabled["selected_ids"] == [202]
        assert disabled["income"] == "20.02"


def test_ungrouped_initial_sync_data_is_hidden_until_character_is_assigned(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group = services.groups.create("Empty Group", clock.now_utc())
        services.journal.commit_sync_batch(
            101,
            [
                JournalEntry(
                    88,
                    101,
                    clock.now_utc(),
                    Decimal(5),
                    "bounty_prizes",
                )
            ],
            clock.now_utc(),
        )

        hidden = client.get(f"/api/dashboard?group_id={group.group_id}").json()
        assert hidden["selected_ids"] == []
        assert hidden["income"] == "0"
        services.characters.set_group(101, group.group_id)
        visible = client.get(f"/api/dashboard?group_id={group.group_id}").json()
        assert visible["selected_ids"] == [101]
        assert visible["income"] == "5"


@pytest.mark.parametrize("route", ["/api/dashboard", "/api/entries"])
@pytest.mark.parametrize(
    "query",
    [
        "character_id=999",
        "character_id=not-an-id",
        "character_id=-1",
        "from=2026-09-31",
        "from=not-a-date",
        "from=20260928",
        "from=2026-09-29&to=2026-09-28",
        "from=2025-01-01&to=2026-01-02",
        "from=9999-12-31&to=9999-12-31",
    ],
)
def test_report_query_validation_is_safe_and_returns_400(context, route, query):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group = services.groups.create("Validation Group", clock.now_utc())
        response = client.get(f"{route}?group_id={group.group_id}&{query}")
        assert response.status_code == 400
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {"code", "message"}


def test_366_day_report_is_accepted(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group = services.groups.create("Long Range", clock.now_utc())
        response = client.get(
            f"/api/dashboard?group_id={group.group_id}&from=2024-01-01&to=2024-12-31"
        )
        assert response.status_code == 200
        assert len(response.json()["daily"]) == 366


@pytest.mark.parametrize("route", ["/api/characters/101/enabled"])
def test_mutations_require_authentication_and_matching_csrf(context, route):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        assert client.post(route, json={"enabled": True}).status_code == 401
        authorize(client, settings)
        for token in [None, "wrong", "非匹配"]:
            # HTTP header values must be ASCII; the Unicode case uses UTF-8 bytes.
            headers = {} if token is None else {"X-CSRF-Token": token.encode()}
            response = client.post(route, json={"enabled": True}, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_failed"
        csrf = session_data(client, settings.app_session_secret)["csrf_token"]
        response = client.post(
            route, json={"enabled": True}, headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200


def test_enabled_mutation_changes_default_report_and_unknown_character_is_404(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        csrf = {
            "X-CSRF-Token": session_data(client, settings.app_session_secret)[
                "csrf_token"
            ]
        }
        response = client.post(
            "/api/characters/202/enabled", json={"enabled": True}, headers=csrf
        )
        assert response.status_code == 200
        assert response.json()["character"]["enabled"] is True
        assert (
            client.get(f"/api/dashboard?group_id={group_id}").json()["income"]
            == "30.12"
        )
        client.post(
            "/api/characters/101/enabled", json={"enabled": False}, headers=csrf
        )
        assert (
            client.get(f"/api/dashboard?group_id={group_id}").json()["income"]
            == "20.02"
        )
        missing = client.post(
            "/api/characters/999/enabled", json={"enabled": True}, headers=csrf
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "unknown_character"


@pytest.mark.parametrize(
    "payload", [{}, {"enabled": "private-invalid"}, {"enabled": 1}, {"enabled": None}]
)
def test_invalid_mutation_body_returns_sanitized_400(context, payload):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        csrf = {
            "X-CSRF-Token": session_data(client, settings.app_session_secret)[
                "csrf_token"
            ]
        }
        response = client.post(
            "/api/characters/101/enabled", json=payload, headers=csrf
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert "private-" not in response.text


def test_sync_requires_authentication_csrf_and_known_group(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        assert client.post("/api/sync?group_id=1").status_code == 401
        authorize(client, settings)
        group = services.groups.create("Sync Group", clock.now_utc())
        assert client.post(f"/api/sync?group_id={group.group_id}").status_code == 403
        headers = csrf_headers(client, settings)
        missing = client.post("/api/sync", headers=headers)
        unknown = client.post("/api/sync?group_id=999", headers=headers)
        assert (missing.status_code, missing.json()["error"]["code"]) == (
            400,
            "invalid_request",
        )
        assert (unknown.status_code, unknown.json()["error"]["code"]) == (
            404,
            "unknown_group",
        )
        response = client.post(f"/api/sync?group_id={group.group_id}", headers=headers)
        assert response.status_code == 200
        assert services.sync.calls[-1] == ("group", group.group_id)


def test_sync_returns_partial_role_results_without_losing_old_report(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group_id = seed_reports(services, clock)
        services.characters.set_enabled(202, True)
        services.characters.mark_authorization_required(202)
        before = client.get(f"/api/dashboard?group_id={group_id}").json()
        services.sync.results = (
            SyncResult(101, 3, clock.now_utc(), "ok", None),
            SyncResult(202, 0, clock.now_utc(), "failed", "authorization_failed"),
        )
        csrf = {
            "X-CSRF-Token": session_data(client, settings.app_session_secret)[
                "csrf_token"
            ]
        }
        response = client.post(f"/api/sync?group_id={group_id}", headers=csrf)
        assert response.status_code == 200
        first, second = response.json()["results"]
        assert (first["character_id"], first["new_count"], first["status"]) == (
            101,
            3,
            "ok",
        )
        assert second["character_id"] == 202 and second["status"] == "failed"
        assert second["error"] == {
            "code": "authorization_failed",
            "message": "角色授权已失效，请重新授权。",
        }
        assert services.sync.calls[-1] == ("group", group_id)
        assert client.get(f"/api/dashboard?group_id={group_id}").json() == before
        assert (
            client.get("/api/characters").json()["characters"][1][
                "authorization_required"
            ]
            is True
        )


def test_sync_arbitrary_error_code_is_not_echoed(context):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group = services.groups.create("Sync Group", clock.now_utc())
        services.sync.results = (
            SyncResult(101, 0, clock.now_utc(), "failed", "private-error-token"),
        )
        csrf = {
            "X-CSRF-Token": session_data(client, settings.app_session_secret)[
                "csrf_token"
            ]
        }
        response = client.post(f"/api/sync?group_id={group.group_id}", headers=csrf)
        assert response.status_code == 200
        assert response.json()["results"][0]["error"]["code"] == "sync_failed"
        assert "private-" not in response.text


@pytest.mark.parametrize(
    "failure, status, code",
    [
        (RuntimeError("private-failure"), 502, "sync_failed"),
        (EsiError("rate_limited", "private-failure"), 429, "rate_limited"),
        (EsiDataError("invalid_data", "private-failure"), 502, "invalid_data"),
    ],
)
def test_service_exceptions_use_stable_safe_errors(context, failure, status, code):
    settings, services, clock = context
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group = services.groups.create("Failure Group", clock.now_utc())
        services.sync.fail = failure
        csrf = {
            "X-CSRF-Token": session_data(client, settings.app_session_secret)[
                "csrf_token"
            ]
        }
        response = client.post(f"/api/sync?group_id={group.group_id}", headers=csrf)
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert "private-" not in response.text
        assert (
            client.get(f"/api/dashboard?group_id={group.group_id}").status_code == 200
        )


def test_missing_sync_configuration_disables_sync(context):
    settings, services, clock = context
    services.sync = None
    with TestClient(create_app(settings, services, clock)) as client:
        authorize(client, settings)
        group = services.groups.create("No Sync Group", clock.now_utc())
        csrf = {
            "X-CSRF-Token": session_data(client, settings.app_session_secret)[
                "csrf_token"
            ]
        }
        response = client.post(f"/api/sync?group_id={group.group_id}", headers=csrf)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "configuration_required"


def test_login_failure_is_sanitized(context):
    settings, services, clock = context
    services.sso.failure = "login"
    with TestClient(
        create_app(settings, services, clock), raise_server_exceptions=False
    ) as client:
        response = client.get("/auth/login")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "authorization_failed"
        assert "private-" not in response.text


def test_malformed_callback_configuration_still_renders_help(context):
    settings, services, clock = context
    settings.eve_callback_url = "https://[private-invalid"
    with TestClient(create_app(settings, services, clock)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "EVE_CALLBACK_URL" in response.text
        assert "private-" not in response.text


def test_repository_error_uses_sanitized_data_error(context, monkeypatch):
    settings, services, clock = context

    def broken_read(*args):
        raise sqlite3.OperationalError("private-database-details")

    with TestClient(
        create_app(settings, services, clock), raise_server_exceptions=False
    ) as client:
        authorize(client, settings)
        group = services.groups.create("Broken Read Group", clock.now_utc())
        monkeypatch.setattr(services.journal, "list_for_report", broken_read)
        response = client.get(f"/api/dashboard?group_id={group.group_id}")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "data_error"
        assert "private-" not in response.text
