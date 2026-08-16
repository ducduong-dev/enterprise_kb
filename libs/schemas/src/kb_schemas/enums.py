"""Domain enumerations. These names are also the Postgres ENUM labels — changing a label
is a migration, not an edit."""

from __future__ import annotations

from enum import StrEnum


class Visibility(StrEnum):
    EXTERNAL = "external"
    INTERNAL_ALL = "internal_all"
    RESTRICTED = "restricted"


class DocStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    EXPIRED = "expired"


class DocClass(StrEnum):
    REGULATORY = "regulatory"
    INTERNAL_NORMATIVE = "internal_normative"
    OPERATIONAL = "operational"
    CUSTOMER_FACING = "customer_facing"


class RefType(StrEnum):
    CITES = "cites"
    AMENDS = "amends"
    ABROGATES = "abrogates"
    IMPLEMENTS = "implements"
    CONSOLIDATES = "consolidates"


class PiiStatus(StrEnum):
    PENDING = "pending"
    CLEAR = "clear"
    BLOCKED = "blocked"
    OVERRIDDEN = "overridden"


class SourceType(StrEnum):
    UPLOAD = "upload"
    PORTAL_EDIT = "portal_edit"
    CONSOLIDATION = "consolidation"


class ReviewTaskType(StrEnum):
    IDP_REVIEW = "idp_review"
    IDENTITY_REVIEW = "identity_review"
    MERGE_REVIEW = "merge_review"
    IMPACT_REVIEW = "impact_review"
    PII_OVERRIDE = "pii_override"
    #: A confirmed expiry is approaching. Opened at T-30 by the sweep so a steward is warned
    #: before an instrument leaves service, rather than after (ADR-0031).
    EXPIRY_REVIEW = "expiry_review"
    #: `documents.review_by` has come round — periodic re-attestation that an internal
    #: procedure is still accurate. A separate queue from the above because it is different
    #: work on a different rhythm.
    PERIODIC_REVIEW = "periodic_review"


class ReviewTaskState(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    DECIDED = "decided"
    CANCELLED = "cancelled"


class ExpiryBasis(StrEnum):
    """Where an expiry date came from — and, from M9c, what it is allowed to do.

    `SELF_STATED` is read from the instrument's own sunset clause at ingest. `ABROGATED_BY` and
    `DECLARED_BY` both arrive from a later instrument: the first from an `abrogates` edge, the
    second from a sentence that names the clauses it ends (ADR-0039). `STEWARD` is a person
    deciding without either.
    """

    SELF_STATED = "self_stated"
    ABROGATED_BY = "abrogated_by"
    DECLARED_BY = "declared_by"
    STEWARD = "steward"


class ExpiryState(StrEnum):
    """Only `CONFIRMED` is ever served. A detector writes `PROPOSED` (ADR-0030)."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REVOKED = "revoked"


class SupersessionBasis(StrEnum):
    """How we came to believe one clause replaced another — and what that permits.

    `DECLARED` is the corpus stating it in the replacing text (ADR-0039); the rest are our own
    conclusions. Only the first is evidence that a rule *ended*, which is why only it writes an
    expiry ledger row alongside (ADR-0040).
    """

    DECLARED = "declared"
    EDGE_ARTICLE = "edge_article"
    DETECTED = "detected"
    STEWARD = "steward"


class DeclarationState(StrEnum):
    """Where a read declaration is between the sentence and the decision.

    `WAITING` is the parked case — the instrument it declares against is not in the registry
    yet, which is routine when an archive is digitised in whatever order it yields (ADR-0028).
    `APPLIED` means the expiry ledger row and the supersession pointer both exist and this row
    is history, kept because "who confirmed that Điều 12 was repealed, and on what sentence"
    must be answerable from one table.
    """

    WAITING = "waiting"
    OPEN = "open"
    APPLIED = "applied"
    REJECTED = "rejected"


class SupersessionVerdict(StrEnum):
    """ADR-0033's four buckets. Four rather than a confidence score because `DIFFERENT_SCOPE`
    and `CONFLICTING_UNRESOLVED` are not weak instances of `SUPERSEDED` — they are different
    kinds of wrong, and a single score collapses them into "below threshold" where they are
    dropped without a trace."""

    SAME_RULE_RESTATED = "same_rule_restated"
    SUPERSEDED = "superseded"
    DIFFERENT_SCOPE = "different_scope"
    CONFLICTING_UNRESOLVED = "conflicting_unresolved"


class PrincipalKind(StrEnum):
    """Who is asking. Drives the entire server-side filter (INV-2/3/4)."""

    USER = "user"
    INTERNAL_BOT = "internal_bot"
    EXTERNAL_BOT = "external_bot"
    SERVICE = "service"


class RetrievalMode(StrEnum):
    CURRENT = "current"
    AS_OF = "as_of"


#: Document classes that can never be published without human approval (INV-8).
#: Enforced by a code guard in the publish service; this constant is the single source.
NO_AUTOMATION_CLASSES: frozenset[DocClass] = frozenset(
    {DocClass.REGULATORY, DocClass.CUSTOMER_FACING}
)

#: Statuses a normal read path may see. Archived/expired require the archive scope.
SERVING_STATUSES: frozenset[DocStatus] = frozenset({DocStatus.PUBLISHED})

#: PII states that permit publication (INV-7). `overridden` requires an audited human act.
PUBLISHABLE_PII_STATUSES: frozenset[PiiStatus] = frozenset({PiiStatus.CLEAR, PiiStatus.OVERRIDDEN})
