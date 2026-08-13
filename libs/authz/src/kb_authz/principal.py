"""The verified caller.

A `Principal` is only ever produced by `kb_authz.tokens` from a verified token or by the test
fixtures. No service constructs one from request data — that would be the widening hole INV-2
exists to close.
"""

from __future__ import annotations

from typing import Self

from kb_schemas.enums import PrincipalKind
from pydantic import BaseModel, ConfigDict, model_validator


class Role:
    """Realm roles that change what a principal may do. Mapped in ops/keycloak-realm/."""

    STEWARD = "kb-steward"
    APPROVER = "kb-approver"
    #: May run point-in-time (`as_of`) retrieval against archived versions (INV-6/INV-9).
    ARCHIVE_READER = "kb-archive-reader"
    #: May override a blocked PII gate, with justification (INV-7).
    PII_OVERRIDER = "kb-pii-overrider"
    LEGAL = "kb-legal"
    ADMIN = "kb-admin"


class Scope:
    """OAuth scopes for service accounts. A service account has no document visibility
    unless it carries one of these, and none of them ever grants `restricted`."""

    #: Read internal_all + external content without a user context. Batch/eval use only.
    RETRIEVE_INTERNAL = "kb.retrieve.internal"
    #: The external bot's hard-scoped grant (INV-4). Bound to the client, not the request.
    RETRIEVE_EXTERNAL = "kb.retrieve.external"
    #: Write to the indexes. Never grants read-through of retrieval.
    INDEX_WRITE = "kb.index.write"


class Principal(BaseModel):
    """Immutable identity of the caller for one request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    kind: PrincipalKind
    display_name: str | None = None
    groups: frozenset[str] = frozenset()
    department: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    issuer: str | None = None
    token_id: str | None = None
    #: Set only on `internal_bot` principals, via OIDC token exchange (INV-3).
    on_behalf_of: Principal | None = None

    @model_validator(mode="after")
    def _check_delegation(self) -> Self:
        if self.on_behalf_of is not None:
            if self.kind is not PrincipalKind.INTERNAL_BOT:
                raise ValueError("only the internal bot may act on behalf of a user")
            if self.on_behalf_of.kind is not PrincipalKind.USER:
                raise ValueError("on_behalf_of must be a human user")
            if self.on_behalf_of.on_behalf_of is not None:
                raise ValueError("delegation may not be chained")
        return self

    @property
    def effective_user(self) -> Principal | None:
        """The identity whose ACL applies. For the internal bot that is the end user, never
        the bot itself — the bot's own account grants zero document visibility (INV-3)."""
        if self.kind is PrincipalKind.INTERNAL_BOT:
            return self.on_behalf_of
        if self.kind is PrincipalKind.USER:
            return self
        return None

    @property
    def audit_actor(self) -> str:
        return self.subject

    @property
    def audit_on_behalf_of(self) -> str | None:
        return self.on_behalf_of.subject if self.on_behalf_of else None

    def has_role(self, role: str) -> bool:
        """Roles of the effective user, not the delegating bot: a bot cannot lend privileges
        it holds, and cannot borrow the user's beyond what the user has."""
        target = self.effective_user or self
        return role in target.roles

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def describe(self) -> dict[str, object]:
        """Audit-safe summary. No token material."""
        return {
            "subject": self.subject,
            "kind": self.kind.value,
            "groups": sorted(self.groups),
            "department": self.department,
            "roles": sorted(self.roles),
            "scopes": sorted(self.scopes),
            "on_behalf_of": self.audit_on_behalf_of,
        }


Principal.model_rebuild()
