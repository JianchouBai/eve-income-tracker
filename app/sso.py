import base64
import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from app.config import Settings

DISCOVERY_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
WALLET_SCOPE = "esi-wallet.read_character_wallet.v1"
PROJECTS_SCOPE = "esi-corporations.read_projects.v1"
REQUESTED_SCOPES = (WALLET_SCOPE, PROJECTS_SCOPE)
CACHE_LIFETIME = timedelta(hours=1)


class SsoError(Exception):
    def __init__(self, code: str, user_message: str):
        self.code = code
        self.user_message = user_message
        super().__init__(user_message)


@dataclass(frozen=True)
class PkcePair:
    verifier: str = field(repr=False)
    challenge: str

    @classmethod
    def generate(
        cls,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> "PkcePair":
        entropy = random_bytes(32)
        if not isinstance(entropy, bytes) or len(entropy) != 32:
            raise ValueError("PKCE entropy source must return exactly 32 bytes")
        verifier = base64.urlsafe_b64encode(entropy).decode().rstrip("=")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode()
            .rstrip("=")
        )
        return cls(verifier, challenge)


@dataclass(frozen=True)
class SsoMetadata:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class CharacterIdentity:
    character_id: int
    character_name: str
    scopes: tuple[str, ...]


class SsoClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._settings = settings
        self._http = http
        self._clock = clock
        self._metadata: SsoMetadata | None = None
        self._metadata_expires_at: datetime | None = None
        self._jwks: list[dict] | None = None
        self._jwks_expires_at: datetime | None = None

    async def build_authorization_url(self, state: str, challenge: str) -> str:
        self._validate_oauth_value(state)
        self._validate_pkce_value(challenge)
        metadata = await self._discover()
        return (
            metadata.authorization_endpoint
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self._settings.eve_client_id,
                    "redirect_uri": self._settings.eve_callback_url,
                    "scope": " ".join(REQUESTED_SCOPES),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    async def exchange_code(self, code: str, verifier: str) -> TokenSet:
        self._validate_oauth_value(code)
        self._validate_pkce_value(verifier)
        return await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.eve_callback_url,
                "code_verifier": verifier,
                "client_id": self._settings.eve_client_id,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        self._validate_oauth_value(refresh_token)
        return await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._settings.eve_client_id,
            }
        )

    @staticmethod
    def _validate_oauth_value(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise SsoError("invalid_request", "EVE SSO request is invalid.")

    @staticmethod
    def _validate_pkce_value(value: str) -> None:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None
        ):
            raise SsoError("invalid_pkce", "EVE SSO request is invalid.")

    async def verify_access_token(self, access_token: str) -> CharacterIdentity:
        try:
            header = jwt.get_unverified_header(access_token)
        except (jwt.PyJWTError, ValueError, TypeError):
            raise SsoError(
                "malformed_token", "EVE SSO token verification failed."
            ) from None

        algorithm = header.get("alg")
        if algorithm == "ES256":
            raise SsoError("unsupported_es256", "EVE SSO token verification failed.")
        if algorithm != "RS256":
            raise SsoError(
                "unsupported_algorithm", "EVE SSO token verification failed."
            )

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise SsoError("missing_key_id", "EVE SSO token verification failed.")

        keys = await self._get_jwks()
        matches = [key for key in keys if key.get("kid") == kid]
        if len(matches) != 1:
            keys = await self._get_jwks(force_refresh=True)
            matches = [key for key in keys if key.get("kid") == kid]
        if len(matches) != 1:
            raise SsoError(
                "signing_key_not_found", "EVE SSO token verification failed."
            )

        key = matches[0]
        if (
            key.get("kty") != "RSA"
            or key.get("alg", "RS256") != "RS256"
            or key.get("use", "sig") != "sig"
        ):
            raise SsoError("invalid_jwks", "EVE SSO signing keys are invalid.")
        try:
            public_key = jwt.PyJWK.from_dict(key, algorithm="RS256").key
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise SsoError(
                "invalid_jwks", "EVE SSO signing keys are invalid."
            ) from None

        try:
            claims = jwt.decode(
                access_token,
                public_key,
                algorithms=["RS256"],
                issuer=[
                    "https://login.eveonline.com/",
                    "https://login.eveonline.com",
                    "login.eveonline.com",
                ],
                options={
                    "verify_aud": False,
                    "require": ["exp", "iss", "aud", "scp", "sub", "name"],
                },
            )
        except jwt.InvalidSignatureError:
            raise SsoError(
                "invalid_signature", "EVE SSO token verification failed."
            ) from None
        except jwt.ExpiredSignatureError:
            raise SsoError(
                "expired_token", "EVE SSO token verification failed."
            ) from None
        except jwt.ImmatureSignatureError:
            raise SsoError(
                "immature_token", "EVE SSO token verification failed."
            ) from None
        except jwt.InvalidIssuerError:
            raise SsoError(
                "invalid_issuer", "EVE SSO token verification failed."
            ) from None
        except jwt.MissingRequiredClaimError:
            raise SsoError(
                "missing_required_claim", "EVE SSO token verification failed."
            ) from None
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
            raise SsoError(
                "invalid_standard_claims", "EVE SSO token verification failed."
            ) from None

        audience = claims["aud"]
        if (
            not isinstance(audience, list)
            or not all(isinstance(value, str) for value in audience)
            or not {self._settings.eve_client_id, "EVE Online"}.issubset(audience)
        ):
            raise SsoError(
                "invalid_audience_claim", "EVE SSO token verification failed."
            )

        raw_scopes = claims["scp"]
        if isinstance(raw_scopes, str):
            scopes = [raw_scopes]
        elif isinstance(raw_scopes, list) and all(
            isinstance(value, str) for value in raw_scopes
        ):
            scopes = raw_scopes
        else:
            raise SsoError("invalid_scope_format", "EVE SSO token verification failed.")
        if WALLET_SCOPE not in scopes:
            raise SsoError(
                "wallet_scope_not_granted", "EVE SSO token verification failed."
            )

        subject = claims["sub"]
        if (
            not isinstance(subject, str)
            or re.fullmatch(r"CHARACTER:EVE:[1-9][0-9]*", subject) is None
        ):
            raise SsoError(
                "invalid_subject_claim", "EVE SSO token verification failed."
            )

        name = claims["name"]
        if not isinstance(name, str) or not name.strip():
            raise SsoError("invalid_name_claim", "EVE SSO token verification failed.")

        return CharacterIdentity(
            int(subject.removeprefix("CHARACTER:EVE:")), name, tuple(scopes)
        )

    async def _get_jwks(self, *, force_refresh: bool = False) -> list[dict]:
        if (
            not force_refresh
            and self._jwks is not None
            and self._jwks_expires_at is not None
            and self._clock() < self._jwks_expires_at
        ):
            return self._jwks
        metadata = await self._discover()
        payload = await self._request_json("GET", metadata.jwks_uri)
        keys = payload.get("keys")
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(key, dict) for key in keys)
        ):
            raise SsoError("invalid_jwks", "EVE SSO signing keys are invalid.")
        self._jwks = keys
        self._jwks_expires_at = self._clock() + CACHE_LIFETIME
        return keys

    async def _request_tokens(self, data: dict[str, str]) -> TokenSet:
        metadata = await self._discover()
        payload = await self._request_json(
            "POST",
            metadata.token_endpoint,
            data=data,
        )
        try:
            access = payload["access_token"]
            refresh = payload["refresh_token"]
            lifetime = payload["expires_in"]
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(refresh, str)
                or not refresh
                or type(lifetime) is not int
                or lifetime <= 0
                or payload.get("token_type") != "Bearer"
            ):
                raise ValueError
            expires_at = self._clock() + timedelta(seconds=lifetime)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise SsoError(
                "invalid_response", "EVE SSO returned an invalid token response."
            ) from None
        return TokenSet(access, refresh, expires_at)

    async def _discover(self) -> SsoMetadata:
        now = self._clock()
        if (
            self._metadata is not None
            and self._metadata_expires_at is not None
            and now < self._metadata_expires_at
        ):
            return self._metadata
        payload = await self._request_json("GET", DISCOVERY_URL)
        try:
            endpoints = [
                payload[name]
                for name in (
                    "authorization_endpoint",
                    "token_endpoint",
                    "jwks_uri",
                )
            ]
            for endpoint in endpoints:
                self._validate_endpoint(endpoint)
        except (KeyError, TypeError, ValueError):
            raise SsoError("invalid_metadata", "EVE SSO metadata is invalid.") from None
        self._metadata = SsoMetadata(*endpoints)
        self._metadata_expires_at = self._clock() + CACHE_LIFETIME
        return self._metadata

    @staticmethod
    def _validate_endpoint(endpoint: str) -> None:
        if not isinstance(endpoint, str):
            raise TypeError
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "login.eveonline.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in endpoint)
        ):
            raise ValueError

    async def _request_json(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = await self._http.request(
                method,
                url,
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                **kwargs,
            )
        except httpx.HTTPError:
            raise SsoError(
                "unavailable", "EVE SSO is temporarily unavailable."
            ) from None
        if not response.is_success:
            # Preserve only the standard revocation signal, never provider text.
            if method == "POST" and response.status_code == 400:
                try:
                    error = response.json()
                except (ValueError, UnicodeError):
                    error = None
                if isinstance(error, dict) and error.get("error") == "invalid_grant":
                    raise SsoError(
                        "invalid_grant",
                        "EVE authorization has expired or been revoked.",
                    )
            raise SsoError("request_failed", "EVE SSO rejected the request.")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
        except (TypeError, ValueError, UnicodeError):
            raise SsoError(
                "invalid_response", "EVE SSO returned an invalid response."
            ) from None
        return payload
