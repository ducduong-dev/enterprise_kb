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


class ReviewTaskState(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    DECIDED = "decided"
    CANCELLED = "cancelled"


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
