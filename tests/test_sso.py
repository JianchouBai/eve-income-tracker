import asyncio
import base64
import json
import logging
import traceback
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.sso import SsoClient, SsoError

DISCOVERY = "https://login.eveonline.com/.well-known/oauth-authorization-server"
WALLET_SCOPE = "esi-wallet.read_character_wallet.v1"
METADATA = {
    "issuer": "https://login.eveonline.com",
    "authorization_endpoint": "https://login.eveonline.com/v2/oauth/authorize",
    "token_endpoint": "https://login.eveonline.com/v2/oauth/token",
    "jwks_uri": "https://login.eveonline.com/oauth/jwks",
    "response_types_supported": ["code"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    "code_challenge_methods_supported": ["S256"],
    "revocation_endpoint": "https://login.eveonline.com/v2/oauth/revoke",
    "revocation_endpoint_auth_methods_supported": ["client_secret_basic"],
}
SECRET = "client-secret-never-print"
CODE = "authorization-code-never-print"
ACCESS = "access-token-never-print"
REFRESH = "refresh-token-never-print"
TOKEN_RESPONSE = {
    "access_token": ACCESS,
    "token_type": "Bearer",
    "expires_in": 1199,
    "refresh_token": REFRESH,
}


def settings():
    return Settings(
        _env_file=None,
        eve_client_id="client-id",
        eve_client_secret=SECRET,
        eve_callback_url="http://localhost:8000/auth/callback",
    )


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def test_authorization_url_and_metadata_cache_expire_at_one_hour():
    async def scenario():
        requests = []
        clock = Clock()

        def handle(request):
            requests.append(request)
            assert str(request.url) == DISCOVERY
            assert all(v is not None for v in request.extensions["timeout"].values())
            return httpx.Response(200, json=METADATA)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sso = SsoClient(settings(), http, clock)
            parsed = urlsplit(await sso.build_authorization_url("state +/&?"))
            assert (parsed.scheme, parsed.netloc, parsed.path) == (
                "https",
                "login.eveonline.com",
                "/v2/oauth/authorize",
            )
            assert parse_qs(parsed.query) == {
                "response_type": ["code"],
                "client_id": ["client-id"],
                "redirect_uri": ["http://localhost:8000/auth/callback"],
                "scope": [WALLET_SCOPE],
                "state": ["state +/&?"],
            }
            clock.now += timedelta(seconds=3599)
            await sso.build_authorization_url("second")
            assert len(requests) == 1
            clock.now += timedelta(seconds=1)
            await sso.build_authorization_url("third")
            assert len(requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["exchange", "refresh"])
def test_token_request_uses_form_and_basic_auth(operation):
    async def scenario():
        token_requests = []

        def handle(request):
            if str(request.url) == DISCOVERY:
                return httpx.Response(200, json=METADATA)
            token_requests.append(request)
            assert str(request.url) == "https://login.eveonline.com/v2/oauth/token"
            assert request.method == "POST"
            assert (
                request.headers["content-type"] == "application/x-www-form-urlencoded"
            )
            auth = base64.b64decode(
                request.headers["authorization"].split()[1]
            ).decode()
            assert auth == "client-id:" + SECRET
            assert all(v is not None for v in request.extensions["timeout"].values())
            expected = (
                {
                    "grant_type": ["authorization_code"],
                    "code": [CODE],
                    "redirect_uri": ["http://localhost:8000/auth/callback"],
                }
                if operation == "exchange"
                else {"grant_type": ["refresh_token"], "refresh_token": [REFRESH]}
            )
            assert parse_qs(request.content.decode()) == expected
            return httpx.Response(200, json=TOKEN_RESPONSE)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sso = SsoClient(settings(), http, Clock())
            tokens = (
                await sso.exchange_code(CODE)
                if operation == "exchange"
                else await sso.refresh(REFRESH)
            )
            assert tokens.access_token == ACCESS
            assert tokens.refresh_token == REFRESH
            assert tokens.expires_at == datetime(2030, 1, 1, 0, 19, 59, tzinfo=UTC)
            assert ACCESS not in repr(tokens)
            assert REFRESH not in repr(tokens)
            assert len(token_requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["http", "redirect", "network", "json", "shape"])
@pytest.mark.parametrize("operation", ["exchange", "refresh"])
def test_token_failures_do_not_leak_secrets(failure, operation, caplog):
    async def scenario():
        sensitive = f"{SECRET} {CODE} {ACCESS} {REFRESH}"

        def handle(request):
            if str(request.url) == DISCOVERY:
                return httpx.Response(200, json=METADATA)
            if failure == "network":
                raise httpx.ReadTimeout(sensitive, request=request)
            if failure == "json":
                return httpx.Response(200, text=sensitive)
            if failure == "shape":
                return httpx.Response(200, json={"error": sensitive})
            if failure == "redirect":
                return httpx.Response(
                    307, headers={"Location": "https://evil.example/?" + CODE}
                )
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": sensitive}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as http:
            sso = SsoClient(settings(), http, Clock())
            with pytest.raises(SsoError) as caught:
                if operation == "exchange":
                    await sso.exchange_code(CODE)
                else:
                    await sso.refresh(REFRESH)
            assert caught.value.code
            assert caught.value.user_message
            rendered = "".join(traceback.format_exception(caught.value))
            for secret in [SECRET, CODE, ACCESS, REFRESH]:
                assert secret not in str(caught.value)
                assert secret not in rendered
                assert secret not in caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    "error, expected_code",
    [
        ("invalid_grant", "invalid_grant"),
        (SECRET, "request_failed"),
    ],
)
def test_token_rejection_exposes_only_safe_error_code(error, expected_code):
    async def scenario():
        def handle(request):
            if str(request.url) == DISCOVERY:
                return httpx.Response(200, json=METADATA)
            return httpx.Response(
                400, json={"error": error, "error_description": SECRET}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(SsoError) as caught:
                await SsoClient(settings(), http, Clock()).refresh(REFRESH)
            assert caught.value.code == expected_code
            assert SECRET not in repr(caught.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field", ["authorization_endpoint", "token_endpoint", "jwks_uri"]
)
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://login.eveonline.com/v2/oauth/token",
        "https://login.eveonline.com.evil.example/token",
        "https://evil.example/token",
        "https://user:pass@login.eveonline.com/token",
        "https://login.eveonline.com:444/token",
        "https://login.eveonline.com/token?secret=leak",
    ],
)
def test_untrusted_discovery_endpoints_are_rejected_before_use(field, endpoint):
    async def scenario():
        def handle(request):
            assert str(request.url) == DISCOVERY
            return httpx.Response(200, json={**METADATA, field: endpoint})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(SsoError):
                await SsoClient(settings(), http, Clock()).exchange_code(CODE)

    asyncio.run(scenario())


@pytest.fixture(scope="module")
def signing_keys():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


@pytest.fixture
def jwt_clock(monkeypatch):
    clock = Clock()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz)

    monkeypatch.setattr(jwt.api_jwt, "datetime", FrozenDatetime)
    return clock


def claims(**overrides):
    payload = {
        "scp": [WALLET_SCOPE],
        "jti": "unique-token-id",
        "sub": "CHARACTER:EVE:2112345678",
        "azp": "client-id",
        "tenant": "tranquility",
        "tier": "live",
        "region": "world",
        "aud": ["client-id", "EVE Online"],
        "name": "Verified Pilot",
        "owner": "character-owner-hash",
        "iss": "https://login.eveonline.com/",
        "exp": 1893542400,
        "iat": 1893456000,
    }
    payload.update(overrides)
    return payload


def public_jwk(key, kid):
    return {
        **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
    }


def jwt_transport(signing_keys, requests, jwks=None):
    def handle(request):
        requests.append(str(request.url))
        assert all(v is not None for v in request.extensions["timeout"].values())
        if str(request.url) == DISCOVERY:
            return httpx.Response(200, json=METADATA)
        assert str(request.url) == "https://login.eveonline.com/oauth/jwks"
        return httpx.Response(
            200,
            json=jwks
            if jwks is not None
            else {
                "keys": [
                    public_jwk(signing_keys[1], "other-key"),
                    public_jwk(signing_keys[0], "selected-key"),
                ]
            },
        )

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    "issuer", ["https://login.eveonline.com/", "login.eveonline.com"]
)
def test_verified_identity_uses_selected_key_and_signed_claims(
    signing_keys, jwt_clock, issuer
):
    async def scenario():
        token = jwt.encode(
            claims(iss=issuer),
            signing_keys[0],
            algorithm="RS256",
            headers={"kid": "selected-key"},
        )
        async with httpx.AsyncClient(transport=jwt_transport(signing_keys, [])) as http:
            identity = await SsoClient(settings(), http, jwt_clock).verify_access_token(
                token
            )
        assert identity.character_id == 2112345678
        assert identity.character_name == "Verified Pilot"
        assert identity.scopes == (WALLET_SCOPE,)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": ["EVE Online"]},
        {"aud": ["client-id"]},
        {"aud": "client-id EVE Online"},
        {"aud": None},
        {"aud": [[], "EVE Online"]},
        {"iss": "https://login.eveonline.com/" + "evil"},
        {"iss": "https://login.eveonline.com"},
        {"iss": None},
        {"exp": 1893456000},
        {"exp": 1893455999},
        {"exp": None},
        {"exp": "not-a-time"},
        {"scp": []},
        {"scp": ["other-scope"]},
        {"scp": None},
        {"scp": "prefix-" + WALLET_SCOPE},
        {"scp": [WALLET_SCOPE, []]},
        {"sub": "CHARACTER:EVE:0"},
        {"sub": "CHARACTER:EVE:-1"},
        {"sub": "CHARACTER:EVE:01"},
        {"sub": "CHARACTER:EVE:12suffix"},
        {"sub": "CHARACTER:EVE:12\n"},
        {"sub": "CHARACTER:EVE:１２"},
        {"sub": "CORPORATION:EVE:12"},
        {"sub": None},
        {"sub": 12},
        {"name": None},
        {"name": ""},
    ],
)
def test_invalid_signed_claims_are_rejected(signing_keys, jwt_clock, overrides):
    async def scenario():
        token = jwt.api_jws.encode(
            json.dumps(claims(**overrides)).encode(),
            signing_keys[0],
            algorithm="RS256",
            headers={"kid": "selected-key"},
        )
        async with httpx.AsyncClient(transport=jwt_transport(signing_keys, [])) as http:
            with pytest.raises(SsoError):
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["exp", "iss", "aud", "scp", "sub", "name"])
def test_missing_required_claim_is_rejected(signing_keys, jwt_clock, missing):
    async def scenario():
        payload = claims()
        del payload[missing]
        token = jwt.encode(
            payload, signing_keys[0], algorithm="RS256", headers={"kid": "selected-key"}
        )
        async with httpx.AsyncClient(transport=jwt_transport(signing_keys, [])) as http:
            with pytest.raises(SsoError):
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    ["signature", "unknown-kid", "missing-kid", "RS512", "HS256", "none", "malformed"],
)
def test_unverified_tokens_are_rejected_without_disclosing_token(
    signing_keys, jwt_clock, failure, caplog
):
    async def scenario():
        headers = {"kid": "selected-key"}
        algorithm = "RS256"
        key = signing_keys[1] if failure == "signature" else signing_keys[0]
        if failure == "unknown-kid":
            headers = {"kid": "attacker-key"}
        if failure == "missing-kid":
            headers = {}
        if failure in ["RS512", "HS256", "none"]:
            algorithm = failure
            key = (
                SECRET * 2
                if failure == "HS256"
                else (None if failure == "none" else key)
            )
        token = jwt.encode(claims(), key, algorithm=algorithm, headers=headers)
        if failure == "malformed":
            token = ACCESS
        async with httpx.AsyncClient(transport=jwt_transport(signing_keys, [])) as http:
            with pytest.raises(SsoError) as caught:
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)
            assert token not in "".join(traceback.format_exception(caught.value))
            assert token not in caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


def test_jwks_cache_expires_at_one_hour(signing_keys, jwt_clock):
    async def scenario():
        requests = []
        token = jwt.encode(
            claims(),
            signing_keys[0],
            algorithm="RS256",
            headers={"kid": "selected-key"},
        )
        async with httpx.AsyncClient(
            transport=jwt_transport(signing_keys, requests)
        ) as http:
            sso = SsoClient(settings(), http, jwt_clock)
            await sso.verify_access_token(token)
            jwt_clock.now += timedelta(seconds=3599)
            await sso.verify_access_token(token)
            assert requests.count("https://login.eveonline.com/oauth/jwks") == 1
            jwt_clock.now += timedelta(seconds=1)
            await sso.verify_access_token(token)
            assert requests.count("https://login.eveonline.com/oauth/jwks") == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        "missing-keys",
        "bad-keys",
        "duplicate-kid",
        "bad-modulus",
        "wrong-use",
        "wrong-alg",
    ],
)
def test_invalid_jwks_is_rejected(signing_keys, jwt_clock, failure):
    async def scenario():
        key = public_jwk(signing_keys[0], "selected-key")
        jwks = {"keys": [key]}
        if failure == "missing-keys":
            jwks = {}
        elif failure == "bad-keys":
            jwks = {"keys": "invalid"}
        elif failure == "duplicate-kid":
            jwks = {"keys": [key, key]}
        elif failure == "bad-modulus":
            key["n"] = ""
        elif failure == "wrong-use":
            key["use"] = "enc"
        elif failure == "wrong-alg":
            key["alg"] = "RS512"
        token = jwt.encode(
            claims(),
            signing_keys[0],
            algorithm="RS256",
            headers={"kid": "selected-key"},
        )
        async with httpx.AsyncClient(
            transport=jwt_transport(signing_keys, [], jwks)
        ) as http:
            with pytest.raises(SsoError):
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)

    asyncio.run(scenario())
