import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from app.config import Settings

DISCOVERY_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
WALLET_SCOPE = "esi-wallet.read_character_wallet.v1"
CACHE_LIFETIME = timedelta(hours=1)


class SsoError(Exception):
    def __init__(self, code: str, user_message: str):
        self.code = code
        self.user_message = user_message
        super().__init__(user_message)


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

    async def build_authorization_url(self, state: str) -> str:
        metadata = await self._discover()
        return (
            metadata.authorization_endpoint
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self._settings.eve_client_id,
                    "redirect_uri": self._settings.eve_callback_url,
                    "scope": WALLET_SCOPE,
                    "state": state,
                }
            )
        )

    async def exchange_code(self, code: str) -> TokenSet:
        return await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.eve_callback_url,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def verify_access_token(self, access_token: str) -> CharacterIdentity:
        try:
            header = jwt.get_unverified_header(access_token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise ValueError
            keys = await self._get_jwks()
            matches = [key for key in keys if key.get("kid") == kid]
            if len(matches) != 1:
                keys = await self._get_jwks(force_refresh=True)
                matches = [key for key in keys if key.get("kid") == kid]
            if len(matches) != 1:
                raise ValueError
            key = matches[0]
            if (
                key.get("kty") != "RSA"
                or key.get("alg", "RS256") != "RS256"
                or key.get("use", "sig") != "sig"
            ):
                raise ValueError
            public_key = jwt.PyJWK.from_dict(key, algorithm="RS256").key
            claims = jwt.decode(
                access_token,
                public_key,
                algorithms=["RS256"],
                issuer=["https://login.eveonline.com/", "login.eveonline.com"],
                options={
                    "verify_aud": False,
                    "require": ["exp", "iss", "aud", "scp", "sub", "name"],
                },
            )
            audience = claims["aud"]
            scopes = claims["scp"]
            subject = claims["sub"]
            name = claims["name"]
            if (
                not isinstance(audience, list)
                or not all(isinstance(value, str) for value in audience)
                or not {self._settings.eve_client_id, "EVE Online"}.issubset(audience)
                or not isinstance(scopes, list)
                or not all(isinstance(value, str) for value in scopes)
                or WALLET_SCOPE not in scopes
                or not isinstance(subject, str)
                or re.fullmatch(r"CHARACTER:EVE:[1-9][0-9]*", subject) is None
                or not isinstance(name, str)
                or not name.strip()
            ):
                raise ValueError
            return CharacterIdentity(
                int(subject.removeprefix("CHARACTER:EVE:")), name, tuple(scopes)
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
            raise SsoError(
                "invalid_token", "EVE SSO token verification failed."
            ) from None

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
            auth=httpx.BasicAuth(
                self._settings.eve_client_id,
                self._settings.eve_client_secret,
            ),
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
