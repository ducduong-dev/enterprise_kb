"""Token verification and principal resolution.

Keycloak brokers AD/LDAP; we only ever see OIDC tokens. Delegation uses RFC 8693 token
exchange: the internal bot exchanges its own token for one whose `sub` is the end user and
whose `act.sub` is the bot. We surface that as `Principal(kind=internal_bot, on_behalf_of=user)`
so the audit record names both parties (INV-3, INV-11).
"""

from __future__ import annotations

from typing import Any, Protocol

import jwt
from jwt import PyJWKClient
from kb_common.config import IdentitySettings, get_settings
from kb_common.errors import AuthenticationError
from kb_schemas.enums import PrincipalKind

from kb_authz.principal import Principal


class TokenVerifier(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...


class OidcTokenVerifier:
    """Production verifier: RS256 against the realm JWKS, issuer and audience checked."""

    def __init__(self, settings: IdentitySettings | None = None) -> None:
        self._cfg = settings or get_settings().oidc
        self._jwks = PyJWKClient(self._cfg.resolved_jwks_url, cache_keys=True)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._cfg.audience,
                issuer=self._cfg.issuer,
                leeway=self._cfg.leeway_seconds,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(
                "token verification failed", reason=type(exc).__name__
            ) from exc


class SharedSecretVerifier:
    """Dev/test only. Guarded by `KB_OIDC_ALLOW_INSECURE_TOKENS`, which `get_settings()`
    refuses to honour outside dev/test."""

    def __init__(self, secret: str = "kb-test-secret", settings: IdentitySettings | None = None):
        self._cfg = settings or get_settings().oidc
        if not self._cfg.allow_insecure_tokens:
            raise AuthenticationError("shared-secret verification is disabled")
        self._secret = secret

    def verify(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._cfg.audience,
                issuer=self._cfg.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(
                "token verification failed", reason=type(exc).__name__
            ) from exc


def principal_from_claims(
    claims: dict[str, Any], settings: IdentitySettings | None = None
) -> Principal:
    cfg = settings or get_settings().oidc
    subject = claims.get("sub")
    if not subject:
        raise AuthenticationError("token has no subject")

    actor = claims.get("act")  # RFC 8693 delegation
    if isinstance(actor, dict) and actor.get("sub"):
        user = _user_principal(claims, cfg)
        return Principal(
            subject=str(actor["sub"]),
            kind=PrincipalKind.INTERNAL_BOT,
            display_name=str(actor.get("client_id") or cfg.internal_bot_client_id),
            scopes=_scopes(claims),
            issuer=claims.get("iss"),
            token_id=claims.get("jti"),
            on_behalf_of=user,
        )

    azp = claims.get("azp") or claims.get("client_id")
    if azp == cfg.external_bot_client_id:
        return Principal(
            subject=str(subject),
            kind=PrincipalKind.EXTERNAL_BOT,
            display_name=str(azp),
            scopes=_scopes(claims),
            issuer=claims.get("iss"),
            token_id=claims.get("jti"),
        )

    username = str(claims.get("preferred_username") or "")
    is_service_account = username.startswith("service-account-") or (
        not username and azp is not None
    )
    if is_service_account:
        return Principal(
            subject=str(subject),
            kind=PrincipalKind.SERVICE,
            display_name=username or str(azp or subject),
            roles=_roles(claims, cfg),
            scopes=_scopes(claims),
            issuer=claims.get("iss"),
            token_id=claims.get("jti"),
        )

    return _user_principal(claims, cfg)


def _user_principal(claims: dict[str, Any], cfg: IdentitySettings) -> Principal:
    return Principal(
        subject=str(claims["sub"]),
        kind=PrincipalKind.USER,
        display_name=claims.get("preferred_username") or claims.get("name"),
        groups=_groups(claims, cfg),
        department=claims.get(cfg.department_claim),
        roles=_roles(claims, cfg),
        scopes=_scopes(claims),
        issuer=claims.get("iss"),
        token_id=claims.get("jti"),
    )


def _groups(claims: dict[str, Any], cfg: IdentitySettings) -> frozenset[str]:
    raw = claims.get(cfg.groups_claim) or []
    # Keycloak emits group *paths* ("/dept/legal"); ACLs store bare group names.
    return frozenset(str(g).lstrip("/") for g in raw if g)


def _roles(claims: dict[str, Any], cfg: IdentitySettings) -> frozenset[str]:
    roles: set[str] = set()
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        roles.update(str(r) for r in realm_access.get("roles", []))
    roles.update(str(r) for r in claims.get(cfg.roles_claim, []) or [])
    return frozenset(roles)


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    scope = claims.get("scope") or ""
    return frozenset(str(scope).split()) if scope else frozenset()


def resolve_principal(token: str, verifier: TokenVerifier) -> Principal:
    return principal_from_claims(verifier.verify(token))
