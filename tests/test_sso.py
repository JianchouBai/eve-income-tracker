import asyncio
import base64
import hashlib
import json
import logging
import traceback
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.config import Settings
from app.sso import PkcePair, SsoClient, SsoError

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
    "token_endpoint_auth_methods_supported": ["none"],
    "code_challenge_methods_supported": ["S256"],
    "revocation_endpoint": "https://login.eveonline.com/v2/oauth/revoke",
    "revocation_endpoint_auth_methods_supported": ["none"],
}
SECRET = "client-secret-never-print"
CODE = "authorization-code-never-print"
ACCESS = "access-token-never-print"
REFRESH = "refresh-token-never-print"
VERIFIER = "v" * 43
CHALLENGE = "c" * 43
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
        eve_callback_url="http://localhost:8000/auth/callback",
    )


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def test_pkce_pair_uses_32_random_bytes_and_unpadded_s256():
    first = PkcePair.generate()
    second = PkcePair.generate()

    decoded = base64.urlsafe_b64decode(first.verifier + "=")
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(first.verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert len(decoded) == 32
    assert len(first.verifier) == 43
    assert "=" not in first.verifier
    assert first.challenge == expected_challenge
    assert len(first.challenge) == 43
    assert "=" not in first.challenge
    assert first != second
    assert first.verifier not in repr(first)


def test_authorization_url_uses_pkce_and_metadata_cache_expires_at_one_hour():
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
            parsed = urlsplit(
                await sso.build_authorization_url("state +/&?", CHALLENGE)
            )
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
                "code_challenge": [CHALLENGE],
                "code_challenge_method": ["S256"],
            }
            clock.now += timedelta(seconds=3599)
            await sso.build_authorization_url("second", CHALLENGE)
            assert len(requests) == 1
            clock.now += timedelta(seconds=1)
            await sso.build_authorization_url("third", CHALLENGE)
            assert len(requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["exchange", "refresh"])
def test_token_request_uses_public_client_form_without_authorization(operation):
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
            assert "authorization" not in request.headers
            assert SECRET not in request.content.decode()
            assert all(v is not None for v in request.extensions["timeout"].values())
            expected = (
                {
                    "grant_type": ["authorization_code"],
                    "code": [CODE],
                    "redirect_uri": ["http://localhost:8000/auth/callback"],
                    "code_verifier": [VERIFIER],
                    "client_id": ["client-id"],
                }
                if operation == "exchange"
                else {
                    "grant_type": ["refresh_token"],
                    "refresh_token": [REFRESH],
                    "client_id": ["client-id"],
                }
            )
            assert parse_qs(request.content.decode()) == expected
            return httpx.Response(200, json=TOKEN_RESPONSE)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sso = SsoClient(settings(), http, Clock())
            tokens = (
                await sso.exchange_code(CODE, VERIFIER)
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


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        ("empty_state", "invalid_request"),
        ("bad_challenge", "invalid_pkce"),
        ("empty_code", "invalid_request"),
        ("bad_verifier", "invalid_pkce"),
        ("empty_refresh", "invalid_request"),
    ],
)
def test_malformed_public_client_inputs_fail_before_network(operation, expected_code):
    async def scenario():
        requests = []

        def handle(request):
            requests.append(request)
            raise AssertionError("network must not be called")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sso = SsoClient(settings(), http, Clock())
            with pytest.raises(SsoError) as caught:
                if operation == "empty_state":
                    await sso.build_authorization_url("", CHALLENGE)
                elif operation == "bad_challenge":
                    await sso.build_authorization_url("state", "bad=")
                elif operation == "empty_code":
                    await sso.exchange_code("", VERIFIER)
                elif operation == "bad_verifier":
                    await sso.exchange_code(CODE, "short")
                else:
                    await sso.refresh("")
            assert caught.value.code == expected_code
            assert requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["http", "redirect", "network", "json", "shape"])
@pytest.mark.parametrize("operation", ["exchange", "refresh"])
def test_token_failures_do_not_leak_secrets(failure, operation, caplog):
    async def scenario():
        sensitive = f"{SECRET} {CODE} {ACCESS} {REFRESH} {VERIFIER}"

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
                    await sso.exchange_code(CODE, VERIFIER)
                else:
                    await sso.refresh(REFRESH)
            assert caught.value.code
            assert caught.value.user_message
            rendered = "".join(traceback.format_exception(caught.value))
            for secret in [SECRET, CODE, ACCESS, REFRESH, VERIFIER]:
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
                await SsoClient(settings(), http, Clock()).exchange_code(CODE, VERIFIER)

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
    "issuer",
    [
        "https://login.eveonline.com/",
        "https://login.eveonline.com",
        "login.eveonline.com",
    ],
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


def test_verified_identity_accepts_exact_single_scope_string(signing_keys, jwt_clock):
    async def scenario():
        token = jwt.encode(
            claims(scp=WALLET_SCOPE),
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


def test_es256_token_reports_unsupported_transition_without_disclosing_token(
    jwt_clock, caplog
):
    async def scenario():
        signing_key = ec.generate_private_key(ec.SECP256R1())
        token = jwt.encode(
            claims(),
            signing_key,
            algorithm="ES256",
            headers={"kid": "eve-ec-key"},
        )

        async with httpx.AsyncClient() as http:
            with pytest.raises(SsoError) as caught:
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)

        assert caught.value.code == "unsupported_es256"
        assert token not in "".join(traceback.format_exception(caught.value))
        assert token not in caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"aud": ["EVE Online"]}, "invalid_audience_claim"),
        ({"aud": ["client-id"]}, "invalid_audience_claim"),
        ({"aud": "client-id EVE Online"}, "invalid_audience_claim"),
        ({"aud": None}, "missing_required_claim"),
        ({"aud": [[], "EVE Online"]}, "invalid_audience_claim"),
        ({"iss": "https://login.eveonline.com/" + "evil"}, "invalid_issuer"),
        ({"iss": None}, "missing_required_claim"),
        ({"exp": 1893456000}, "expired_token"),
        ({"exp": 1893455999}, "expired_token"),
        ({"exp": None}, "missing_required_claim"),
        ({"exp": "not-a-time"}, "invalid_standard_claims"),
        ({"iat": 1893456001}, "immature_token"),
        ({"scp": []}, "wallet_scope_not_granted"),
        ({"scp": ["other-scope"]}, "wallet_scope_not_granted"),
        ({"scp": None}, "missing_required_claim"),
        ({"scp": "prefix-" + WALLET_SCOPE}, "wallet_scope_not_granted"),
        ({"scp": [WALLET_SCOPE, []]}, "invalid_scope_format"),
        ({"sub": "CHARACTER:EVE:0"}, "invalid_subject_claim"),
        ({"sub": "CHARACTER:EVE:-1"}, "invalid_subject_claim"),
        ({"sub": "CHARACTER:EVE:01"}, "invalid_subject_claim"),
        ({"sub": "CHARACTER:EVE:12suffix"}, "invalid_subject_claim"),
        ({"sub": "CHARACTER:EVE:12\n"}, "invalid_subject_claim"),
        ({"sub": "CHARACTER:EVE:１２"}, "invalid_subject_claim"),
        ({"sub": "CORPORATION:EVE:12"}, "invalid_subject_claim"),
        ({"sub": None}, "missing_required_claim"),
        ({"sub": 12}, "invalid_standard_claims"),
        ({"name": None}, "missing_required_claim"),
        ({"name": ""}, "invalid_name_claim"),
    ],
)
def test_invalid_signed_claims_report_safe_category(
    signing_keys, jwt_clock, overrides, expected_code
):
    async def scenario():
        token = jwt.api_jws.encode(
            json.dumps(claims(**overrides)).encode(),
            signing_keys[0],
            algorithm="RS256",
            headers={"kid": "selected-key"},
        )
        async with httpx.AsyncClient(transport=jwt_transport(signing_keys, [])) as http:
            with pytest.raises(SsoError) as caught:
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)
            assert caught.value.code == expected_code

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
            with pytest.raises(SsoError) as caught:
                await SsoClient(settings(), http, jwt_clock).verify_access_token(token)
            assert caught.value.code == "missing_required_claim"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("signature", "invalid_signature"),
        ("unknown-kid", "signing_key_not_found"),
        ("missing-kid", "missing_key_id"),
        ("RS512", "unsupported_algorithm"),
        ("HS256", "unsupported_algorithm"),
        ("none", "unsupported_algorithm"),
        ("malformed", "malformed_token"),
    ],
)
def test_unverified_tokens_are_rejected_without_disclosing_token(
    signing_keys, jwt_clock, failure, expected_code, caplog
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
            assert caught.value.code == expected_code
            assert token not in "".join(traceback.format_exception(caught.value))
            assert token not in caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


@pytest.mark.parametrize("cached_matches", [0, 2])
def test_new_kid_refreshes_cached_jwks_once_and_verifies_rotated_key(
    signing_keys, jwt_clock, cached_matches
):
    async def scenario():
        requests = []
        old_key = public_jwk(signing_keys[0], "old-key")
        new_key = public_jwk(signing_keys[1], "new-key")
        old_token = jwt.encode(
            claims(), signing_keys[0], algorithm="RS256", headers={"kid": "old-key"}
        )
        new_token = jwt.encode(
            claims(), signing_keys[1], algorithm="RS256", headers={"kid": "new-key"}
        )

        def handle(request):
            requests.append(str(request.url))
            if str(request.url) == DISCOVERY:
                return httpx.Response(200, json=METADATA)
            assert str(request.url) == METADATA["jwks_uri"]
            keys = (
                [old_key] + [new_key] * cached_matches
                if requests.count(METADATA["jwks_uri"]) == 1
                else [new_key]
            )
            return httpx.Response(200, json={"keys": keys})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sso = SsoClient(settings(), http, jwt_clock)
            await sso.verify_access_token(old_token)
            identity = await sso.verify_access_token(new_token)
            assert identity.character_id == 2112345678
            assert identity.character_name == "Verified Pilot"
            assert requests == [DISCOVERY, METADATA["jwks_uri"], METADATA["jwks_uri"]]
            await sso.verify_access_token(new_token)
            assert len(requests) == 3

    asyncio.run(scenario())


def test_still_unknown_kid_fails_safely_after_one_forced_refresh(
    signing_keys, jwt_clock, caplog
):
    async def scenario():
        requests = []
        known_token = jwt.encode(
            claims(),
            signing_keys[0],
            algorithm="RS256",
            headers={"kid": "selected-key"},
        )
        unknown_token = jwt.encode(
            claims(), signing_keys[0], algorithm="RS256", headers={"kid": "unknown-key"}
        )
        async with httpx.AsyncClient(
            transport=jwt_transport(signing_keys, requests)
        ) as http:
            sso = SsoClient(settings(), http, jwt_clock)
            await sso.verify_access_token(known_token)
            with pytest.raises(SsoError) as caught:
                await sso.verify_access_token(unknown_token)
            assert caught.value.code == "signing_key_not_found"
            assert requests == [DISCOVERY, METADATA["jwks_uri"], METADATA["jwks_uri"]]
            assert unknown_token not in "".join(
                traceback.format_exception(caught.value)
            )
            assert unknown_token not in caplog.text

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
