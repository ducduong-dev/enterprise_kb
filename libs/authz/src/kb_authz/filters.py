"""FilterBuilder — the single place where "what may this caller see" is decided (INV-2).

Rules this module exists to guarantee:

* The filter is derived from the *verified principal*, never from request data.
* Request facets may only narrow. Any facet that would admit a document the base filter
  excludes raises `PolicyViolation` — it is not silently clamped, because silent clamping
  hides probing.
* A principal that resolves to zero visibility never produces an "empty" (= unfiltered)
  filter; it raises. An empty filter must never be mistakable for "no filter".
* The result is frozen and carries a deterministic `filter_id` so the audit record and the
  response can be joined later (INV-11).

The filter is a *description*; `compile.py` turns it into a WHERE clause or an index
filter context. Nothing post-filters results in Python.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from kb_common.errors import AuthzError, PolicyViolation
from kb_schemas.api import Facets
from kb_schemas.enums import (
    DocClass,
    DocStatus,
    PrincipalKind,
    RetrievalMode,
    Visibility,
)

from kb_authz.principal import Principal, Role, Scope

#: Statuses served on the default path (INV-6). `as_of` adds archived/expired.
_CURRENT_STATUSES = frozenset({DocStatus.PUBLISHED})
_ARCHIVE_STATUSES = frozenset({DocStatus.PUBLISHED, DocStatus.ARCHIVED, DocStatus.EXPIRED})


@dataclass(frozen=True, slots=True)
class ResolvedFilter:
    """The server-side ACL predicate, fully resolved. Immutable by construction."""

    principal_ref: str
    on_behalf_of: str | None
    principal_kind: PrincipalKind
    visibilities: frozenset[Visibility]
    #: Groups usable to unlock `restricted` documents. Empty means: no restricted access.
    group_scope: frozenset[str]
    statuses: frozenset[DocStatus]
    #: None = no department narrowing. A set narrows to those departments.
    departments: frozenset[str] | None
    #: None = whole tree. Values are ltree prefixes matched with `<@`.
    category_prefixes: frozenset[str] | None
    doc_classes: frozenset[DocClass] | None
    mode: RetrievalMode
    #: The date at which effectivity is evaluated (today, or the as-of date).
    effective_on: date
    issued_from: date | None = None
    issued_to: date | None = None
    include_tombstoned: bool = False

    def __post_init__(self) -> None:
        if not self.visibilities:
            raise AuthzError("resolved filter admits nothing", principal=self.principal_ref)
        if Visibility.RESTRICTED in self.visibilities and not self.group_scope:
            raise AuthzError(
                "restricted visibility without group scope", principal=self.principal_ref
            )

    def as_dict(self) -> dict[str, Any]:
        """Canonical, sorted form. Goes verbatim into `audit_log.resolved_filter`."""
        return {
            "principal": self.principal_ref,
            "on_behalf_of": self.on_behalf_of,
            "principal_kind": self.principal_kind.value,
            "visibilities": sorted(v.value for v in self.visibilities),
            "group_scope": sorted(self.group_scope),
            "statuses": sorted(s.value for s in self.statuses),
            "departments": sorted(self.departments) if self.departments is not None else None,
            "category_prefixes": (
                sorted(self.category_prefixes) if self.category_prefixes is not None else None
            ),
            "doc_classes": (
                sorted(c.value for c in self.doc_classes) if self.doc_classes is not None else None
            ),
            "mode": self.mode.value,
            "effective_on": self.effective_on.isoformat(),
            "issued_from": self.issued_from.isoformat() if self.issued_from else None,
            "issued_to": self.issued_to.isoformat() if self.issued_to else None,
            "include_tombstoned": self.include_tombstoned,
        }

    @property
    def filter_id(self) -> str:
        """Stable across processes: same principal + same narrowing = same id."""
        blob = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def audit_payload(self) -> dict[str, Any]:
        return {"filter_id": self.filter_id, **self.as_dict()}


class FilterBuilder:
    """Builds a `ResolvedFilter` from a principal, then narrows it with request facets."""

    def __init__(self, today: date | None = None) -> None:
        # Injectable so tests are not clock-dependent.
        self._today = today

    def _now(self) -> date:
        return self._today or date.today()

    # ---------------------------------------------------------------- base filter

    def base(self, principal: Principal) -> ResolvedFilter:
        match principal.kind:
            case PrincipalKind.EXTERNAL_BOT:
                return self._external_bot(principal)
            case PrincipalKind.INTERNAL_BOT:
                return self._internal_bot(principal)
            case PrincipalKind.USER:
                return self._user(principal, principal)
            case PrincipalKind.SERVICE:
                return self._service(principal)

    def _external_bot(self, principal: Principal) -> ResolvedFilter:
        """INV-4: the scope is bound to the service account, server-side. The request has no
        way to express anything else, and we do not consult the token's scopes for widening —
        only to confirm the account is provisioned for external serving at all."""
        if not principal.has_scope(Scope.RETRIEVE_EXTERNAL):
            raise AuthzError(
                "external bot account is not provisioned for external retrieval",
                principal=principal.subject,
            )
        return ResolvedFilter(
            principal_ref=principal.subject,
            on_behalf_of=None,
            principal_kind=principal.kind,
            visibilities=frozenset({Visibility.EXTERNAL}),
            group_scope=frozenset(),
            statuses=frozenset({DocStatus.PUBLISHED}),
            departments=None,
            category_prefixes=None,
            doc_classes=None,
            mode=RetrievalMode.CURRENT,
            effective_on=self._now(),
        )

    def _internal_bot(self, principal: Principal) -> ResolvedFilter:
        """INV-3: the bot has zero visibility of its own; it must carry an exchanged user
        token. Calling without one is a policy violation, not an empty result."""
        user = principal.on_behalf_of
        if user is None:
            raise PolicyViolation(
                "internal bot must act on behalf of an end user",
                principal=principal.subject,
                invariant="INV-3",
            )
        return self._user(user, principal)

    def _user(self, user: Principal, caller: Principal) -> ResolvedFilter:
        visibilities = {Visibility.EXTERNAL, Visibility.INTERNAL_ALL}
        if user.groups:
            visibilities.add(Visibility.RESTRICTED)
        return ResolvedFilter(
            principal_ref=caller.subject,
            on_behalf_of=user.subject if caller is not user else None,
            principal_kind=caller.kind,
            visibilities=frozenset(visibilities),
            group_scope=frozenset(user.groups),
            statuses=_CURRENT_STATUSES,
            departments=None,
            category_prefixes=None,
            doc_classes=None,
            mode=RetrievalMode.CURRENT,
            effective_on=self._now(),
        )

    def _service(self, principal: Principal) -> ResolvedFilter:
        """Service accounts never see `restricted` content: there is no group to attribute the
        access to, and no human to hold accountable. Batch jobs that need restricted content
        run under a real user via token exchange."""
        if not principal.has_scope(Scope.RETRIEVE_INTERNAL):
            raise AuthzError("service account has no retrieval scope", principal=principal.subject)
        return ResolvedFilter(
            principal_ref=principal.subject,
            on_behalf_of=None,
            principal_kind=principal.kind,
            visibilities=frozenset({Visibility.EXTERNAL, Visibility.INTERNAL_ALL}),
            group_scope=frozenset(),
            statuses=_CURRENT_STATUSES,
            departments=None,
            category_prefixes=None,
            doc_classes=None,
            mode=RetrievalMode.CURRENT,
            effective_on=self._now(),
        )

    # ------------------------------------------------------------------ narrowing

    def build(
        self,
        principal: Principal,
        *,
        facets: Facets | None = None,
        mode: RetrievalMode = RetrievalMode.CURRENT,
        as_of_date: date | None = None,
    ) -> ResolvedFilter:
        """Base filter + request narrowing. The only entry point services should call."""
        resolved = self.base(principal)
        if mode is RetrievalMode.AS_OF:
            resolved = self._apply_as_of(principal, resolved, as_of_date)
        if facets is not None:
            resolved = self.narrow(resolved, facets)
        return resolved

    def _apply_as_of(
        self, principal: Principal, resolved: ResolvedFilter, as_of_date: date | None
    ) -> ResolvedFilter:
        """Point-in-time lookup reaches archived versions, so it is a privileged path
        (INV-6/INV-9) and every hit is audited by the caller."""
        if as_of_date is None:
            raise PolicyViolation("as_of mode requires as_of_date", principal=principal.subject)
        if principal.kind is PrincipalKind.EXTERNAL_BOT:
            raise PolicyViolation(
                "external surface may not query archived content",
                principal=principal.subject,
                invariant="INV-4",
            )
        if not principal.has_role(Role.ARCHIVE_READER):
            raise PolicyViolation(
                "principal lacks the archive-reader role",
                principal=principal.subject,
                invariant="INV-6",
            )
        if as_of_date > self._now():
            raise PolicyViolation(
                "as_of_date may not be in the future", principal=principal.subject
            )
        return replace(
            resolved,
            mode=RetrievalMode.AS_OF,
            effective_on=as_of_date,
            statuses=frozenset(resolved.statuses | _ARCHIVE_STATUSES),
        )

    def narrow(self, resolved: ResolvedFilter, facets: Facets) -> ResolvedFilter:
        """Apply request facets to an already-resolved filter.

        Idempotent and monotonic: narrowing an already-narrowed filter can only shrink it.
        chat-api uses this to push condensation-derived facets onto the user's filter.
        """
        updates: dict[str, Any] = {}

        if facets.category is not None:
            updates["category_prefixes"] = self._narrow_category(
                resolved.category_prefixes, facets.category, resolved
            )

        if facets.department is not None:
            updates["departments"] = self._narrow_set(
                resolved.departments, facets.department, resolved, field="department"
            )

        if facets.doc_class is not None:
            try:
                requested_class = DocClass(facets.doc_class)
            except ValueError as exc:
                raise PolicyViolation(
                    f"unknown doc_class {facets.doc_class!r}", principal=resolved.principal_ref
                ) from exc
            narrowed = self._narrow_set(
                resolved.doc_classes, requested_class, resolved, field="doc_class"
            )
            updates["doc_classes"] = narrowed

        if facets.date_from is not None:
            updates["issued_from"] = max(
                filter(None, (resolved.issued_from, facets.date_from)), default=facets.date_from
            )
        if facets.date_to is not None:
            updates["issued_to"] = min(
                filter(None, (resolved.issued_to, facets.date_to)), default=facets.date_to
            )

        return replace(resolved, **updates) if updates else resolved

    @staticmethod
    def _narrow_category(
        current: frozenset[str] | None, requested: str, resolved: ResolvedFilter
    ) -> frozenset[str]:
        requested = requested.strip().strip(".")
        if not requested or not all(
            part and part.replace("_", "").isalnum() for part in requested.split(".")
        ):
            raise PolicyViolation(
                f"invalid category path {requested!r}", principal=resolved.principal_ref
            )
        if current is None:
            return frozenset({requested})
        # Keep only allowed subtrees that lie inside the requested one; if the request points
        # outside every allowed subtree it is a widening attempt.
        kept = {
            allowed
            for allowed in current
            if allowed == requested or allowed.startswith(f"{requested}.")
        }
        if kept:
            return frozenset(kept)
        if any(requested.startswith(f"{allowed}.") for allowed in current):
            return frozenset({requested})  # strictly deeper than an allowed subtree: narrowing
        raise PolicyViolation(
            "category facet falls outside the permitted subtree",
            principal=resolved.principal_ref,
            requested=requested,
            permitted=sorted(current),
            invariant="INV-2",
        )

    @staticmethod
    def _narrow_set[T](
        current: frozenset[T] | None, requested: T, resolved: ResolvedFilter, *, field: str
    ) -> frozenset[T]:
        if current is None:
            return frozenset({requested})
        if requested in current:
            return frozenset({requested})
        raise PolicyViolation(
            f"{field} facet is outside the permitted set",
            principal=resolved.principal_ref,
            field=field,
            requested=str(requested),
            permitted=sorted(str(c) for c in current),
            invariant="INV-2",
        )


def build_filter(
    principal: Principal,
    *,
    facets: Facets | None = None,
    mode: RetrievalMode = RetrievalMode.CURRENT,
    as_of_date: date | None = None,
    today: date | None = None,
) -> ResolvedFilter:
    """Convenience wrapper for call sites that do not hold a builder."""
    return FilterBuilder(today=today).build(
        principal, facets=facets, mode=mode, as_of_date=as_of_date
    )
