"""Request authentication for the portal.

Resolves a verified `Principal` from the bearer token. Nothing downstream may construct one
from request data (INV-2), so this is the only place a portal request acquires an identity.

The portal is an internal surface: it serves stewards, reviewers and approvers. Document
*content* access still goes through retrieval-api's filter; this layer decides who may act.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, Header
from kb_authz.principal import Principal, Role
from kb_authz.tokens import (
    OidcTokenVerifier,
    SharedSecretVerifier,
    TokenVerifier,
    resolve_principal,
)
from kb_common.config import Settings, get_settings
from kb_common.errors import AuthenticationError, AuthzError


@lru_cache(maxsize=1)
def _verifier() -> TokenVerifier:
    settings: Settings = get_settings()
    if settings.oidc.allow_insecure_tokens and not settings.is_production:
        # Dev and tests only; get_settings() refuses this combination in staging/prod.
        return SharedSecretVerifier()
    return OidcTokenVerifier(settings.oidc)


def reset_verifier_cache() -> None:
    """Tests only."""
    _verifier.cache_clear()


def current_principal(authorization: str = Header(default="")) -> Principal:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("a bearer token is required")
    return resolve_principal(token, _verifier())


def require_role(role: str) -> Callable[[Principal], Principal]:
    """Dependency factory for endpoints that need a specific role (approve, override, admin).

    `kb-admin` is accepted everywhere as the break-glass operator role — note that this grants
    no extra *document visibility* (INV-2), only the ability to act.
    """

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_role(role) and not principal.has_role(Role.ADMIN):
            raise AuthzError("missing required role", required=role, subject=principal.subject)
        return principal

    return dependency
