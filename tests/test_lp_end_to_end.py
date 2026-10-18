import asyncio
from datetime import timedelta

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_lp_esi import detail
from test_lp_storage import T1
from test_web import FakeClock, authorize, csrf_headers

from app.config import Settings
from app.esi import EsiClient
from app.lp_storage import LpRepository
from app.lp_sync import LpSyncService
from app.security import TokenCipher
from app.sso import REQUESTED_SCOPES, CharacterIdentity, TokenSet
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    Database,
    JournalRepository,
)
from app.sync import SyncService
from app.web import AppServices, create_app


def test_two_roles_lp_income_survives_retry_restart_and_project_permission_loss(
    tmp_path,
):
    settings = Settings(
        _env_file=None,
        eve_client_id="test-client",
        app_session_secret="isolated-session-secret" * 3,
        app_encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "income.sqlite3",
        eve_user_agent="eve-income-tracker/0.1 (test@example.com)",
    )
    clock = FakeClock()
    clock.current = T1
    values, forbidden = {101: 100, 202: 100}, set()

    class Sso:
        async def build_authorization_url(self, state, challenge):
            return "https://login.eveonline.com/v2/oauth/authorize?state=" + state

        async def exchange_code(self, code, verifier):
            return TokenSet("access-101", "101", T1 + timedelta(minutes=20))

        async def refresh(self, token):
            return TokenSet("access-" + token, token, T1 + timedelta(minutes=20))

        async def verify_access_token(self, token):
            cid = int(token.removeprefix("access-"))
            return CharacterIdentity(cid, f"Pilot {cid}", REQUESTED_SCOPES)

    def handle(request):
        path = request.url.path
        token = request.headers.get("Authorization", "")
        if "/wallet/journal/" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "date": T1.isoformat(),
                        "amount": 10,
                        "ref_type": "bounty_prizes",
                        "description": "test reward",
                    }
                ],
            )
        if path.startswith("/characters/"):
            return httpx.Response(200, json={"corporation_id": 9001})
        cid = int(token.removeprefix("Bearer access-"))
        if cid in forbidden:
            return httpx.Response(403, json={"error": "test project permission"})
        if path.endswith("/projects"):
            return httpx.Response(200, json={"projects": [detail()]})
        if "/contribution/" in path:
            assert path.endswith("/" + str(cid))
            return httpx.Response(200, json={"contributed": values[cid]})
        return httpx.Response(200, json=detail())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    def make_app():
        database = Database(settings.database_path)
        chars, groups, journal = (
            CharacterRepository(database),
            CharacterGroupRepository(database),
            JournalRepository(database),
        )
        cipher, sso, esi = (
            TokenCipher(settings.app_encryption_key),
            Sso(),
            EsiClient(settings, http),
        )
        lp = LpRepository(database)
        sync = SyncService(
            chars,
            journal,
            cipher,
            sso,
            esi,
            clock.now_utc,
            lp=LpSyncService(lp, esi, clock.now_utc),
        )
        services = AppServices(chars, groups, journal, cipher, sso, sync, lp)
        return create_app(settings, services, clock), services

    try:
        app, services = make_app()
        with TestClient(app) as client:
            assert authorize(client, settings).status_code == 302
            group = services.groups.create("LP group", T1)
            services.characters.upsert_authorization(
                202,
                "Pilot 202",
                services.cipher.encrypt("202"),
                T1,
                granted_scopes=REQUESTED_SCOPES,
            )
            for cid in values:
                services.characters.set_group(cid, group.group_id)
            url = f"/api/sync?group_id={group.group_id}"
            client.post(url, headers=csrf_headers(client, settings)).raise_for_status()
            assert (
                client.get(f"/api/dashboard?group_id={group.group_id}").json()["lp"][
                    "net"
                ]
                == "0"
            )
            values.update({101: 200, 202: 200})
            client.post(url, headers=csrf_headers(client, settings)).raise_for_status()
            assert (
                client.get(f"/api/dashboard?group_id={group.group_id}").json()["lp"][
                    "net"
                ]
                == "190"
            )
            client.post(url, headers=csrf_headers(client, settings)).raise_for_status()
            assert (
                client.get(f"/api/dashboard?group_id={group.group_id}").json()["lp"][
                    "net"
                ]
                == "190"
            )

        app, services = make_app()
        with TestClient(app) as client:
            authorize(client, settings)
            assert (
                client.get(f"/api/dashboard?group_id={group.group_id}").json()["lp"][
                    "net"
                ]
                == "190"
            )
            forbidden.add(202)
            response = client.post(url, headers=csrf_headers(client, settings)).json()
            denied = next(r for r in response["results"] if r["character_id"] == 202)
            assert denied["status"] == "ok"
            assert denied["lp"]["status"] == "unavailable"
            assert not services.characters.list_all()[1].authorization_required
            report = client.get(f"/api/dashboard?group_id={group.group_id}").json()
            assert report["lp"]["net"] == "190"
            assert report["lp"]["has_coverage"] and not report["lp"]["complete"]
    finally:
        asyncio.run(http.aclose())
