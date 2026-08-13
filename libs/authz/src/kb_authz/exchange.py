"""RFC 8693 token exchange — how the internal bot borrows a user's visibility (INV-3).

The internal bot is a piece of software with a service account, and that account can see
nothing. When someone asks it a question, it exchanges its own token for one whose `sub` is
that person and whose `act.sub` is the bot. Retrieval then builds the filter from the *user's*
groups, and the audit record names both parties.

Why not simply let the front end hand the bot the user's access token? Because then the bot
holds a credential it can replay anywhere, for as long as it lives, against any service that
accepts it. The exchanged token is issued for this delegation, carries the actor claim that
records it, and is refused by Keycloak if the bot is not permitted to impersonate that user.

The exchange is behind a protocol for the same reason the models are (INV-12): a chat request
should be testable without a Keycloak round trip, and the failure mode of the real one — the
bot is not authorized to act for this user — has to be exercised in tests rather than
discovered in staging.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from kb_common.config import IdentitySettings, get_settings
from kb_common.errors import AuthzError, ConfigError, UpstreamError
from kb_common.logging import get_logger

log = get_logger(__name__)

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
#: Stop using a cached token this long before it expires, so a request that takes a moment
#: does not arrive with one that died in flight.
EXPIRY_MARGIN_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ExchangedToken:
    token: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return time.time() < self.expires_at - EXPIRY_MARGIN_SECONDS


class TokenExchanger(Protocol):
    def exchange(self, subject_token: str, *, user_subject: str) -> str: ...


class KeycloakTokenExchanger:
    """Production exchanger. One HTTP call per user, cached until shortly before expiry."""

    def __init__(
        self,
        settings: IdentitySettings | None = None,
        *,
        client_secret: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._cfg = settings or get_settings().oidc
        if not self._cfg.token_endpoint:
            raise ConfigError("token exchange needs KB_OIDC_TOKEN_ENDPOINT")
        self._secret = client_secret
        self._client = httpx.Client(timeout=timeout)
        self._cache: dict[str, ExchangedToken] = {}

    def exchange(self, subject_token: str, *, user_subject: str) -> str:
        cached = self._cache.get(user_subject)
        if cached is not None and cached.usable:
            return cached.token

        form: dict[str, str] = {
            "grant_type": GRANT_TYPE,
            "client_id": self._cfg.internal_bot_client_id,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "requested_subject": user_subject,
            "audience": self._cfg.audience,
        }
        if self._secret:
            form["client_secret"] = self._secret

        try:
            response = self._client.post(str(self._cfg.token_endpoint), data=form)
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("token exchange unreachable") from exc

        if response.status_code in (400, 401, 403):
            # The bot is not permitted to act for this user. That is an authorization answer,
            # not a transport failure, and it must not degrade into "answer as the bot".
            raise AuthzError(
                "token exchange refused",
                user_subject=user_subject,
                status=response.status_code,
                invariant="INV-3",
            )
        if response.status_code >= 400:  # pragma: no cover - upstream fault
            raise UpstreamError("token exchange failed", status=response.status_code)

        payload: dict[str, Any] = response.json()
        token = str(payload.get("access_token") or "")
        if not token:  # pragma: no cover - upstream fault
            raise UpstreamError("token exchange returned no access token")
        lifetime = float(payload.get("expires_in") or 60)
        self._cache[user_subject] = ExchangedToken(token=token, expires_at=time.time() + lifetime)
        log.info("token_exchanged", extra={"user_subject": user_subject, "expires_in": lifetime})
        return token


@dataclass
class StaticTokenExchanger:
    """Test double. Returns a prepared token per user and records what it was asked for."""

    tokens: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: Users the exchange is not permitted for. Refused exactly as Keycloak refuses.
    refuse: frozenset[str] = frozenset()

    def exchange(self, subject_token: str, *, user_subject: str) -> str:
        self.calls.append((subject_token, user_subject))
        if user_subject in self.refuse:
            raise AuthzError("token exchange refused", user_subject=user_subject, invariant="INV-3")
        return self.tokens.get(user_subject, f"exchanged-for-{user_subject}")
