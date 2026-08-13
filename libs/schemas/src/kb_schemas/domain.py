"""Domain models shared across services.

These mirror the tables in the initial migration. Services exchange these, never ORM rows.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kb_schemas.enums import (
    DocClass,
    DocStatus,
    PiiStatus,
    RefType,
    ReviewTaskState,
    ReviewTaskType,
    SourceType,
    Visibility,
)


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=False)


class Category(Base):
    """Node of the `ltree` category tree, e.g. `regulations.sbv.capital_adequacy`."""

    path: str
    label: str
    default_visibility: Visibility = Visibility.INTERNAL_ALL
    default_allowed_groups: list[str] = Field(default_factory=list)
    steward_group: str | None = None
    #: [OPEN]-3: Compliance to specify per category. Default false = deny-by-not-found,
    #: so an unauthorized principal cannot learn that a document exists (INV-10).
    existence_disclosure: bool = False

    @field_validator("path")
    @classmethod
    def _validate_ltree(cls, value: str) -> str:
        parts = value.split(".")
        if not all(part and part.replace("_", "").isalnum() for part in parts):
            raise ValueError(f"invalid ltree path: {value!r}")
        return value


class Document(Base):
    id: UUID
    title: str
    legal_number: str | None = None
    doc_class: DocClass
    category_path: str
    department: str | None = None
    visibility: Visibility
    allowed_groups: list[str] = Field(default_factory=list)
    status: DocStatus
    canonical_version_id: UUID | None = None
    review_by: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def requires_human_approval(self) -> bool:
        """INV-8 — regulatory and customer_facing never auto-publish."""
        from kb_schemas.enums import NO_AUTOMATION_CLASSES

        return self.doc_class in NO_AUTOMATION_CLASSES


class DocumentVersion(Base):
    """Immutable (INV-9). Only `is_canonical` and `pii_status` transition after creation."""

    id: UUID
    document_id: UUID
    content_ref: str | None = None
    content_hash: str | None = None
    source_type: SourceType = SourceType.UPLOAD
    author: str | None = None
    change_summary: str | None = None
    idp_report_ref: str | None = None
    pii_status: PiiStatus = PiiStatus.PENDING
    effective_from: date | None = None
    effective_to: date | None = None
    is_canonical: bool = False
    retention_until: date | None = None
    created_at: datetime | None = None


class DocumentRef(Base):
    """Reference edge. Targets Document IDs, never version IDs (INV-10)."""

    id: UUID
    src_document_id: UUID
    dst_document_id: UUID
    ref_type: RefType
    articles: list[int] = Field(default_factory=list)
    detected_by: str | None = None
    confirmed_by: str | None = None
    created_at: datetime | None = None


class Chunk(Base):
    """A retrievable unit. Carries a denormalized copy of the ACL so the index can filter
    inside the query (INV-2) without joining the registry."""

    id: UUID
    document_id: UUID
    version_id: UUID
    section_path: str | None = None
    citation_label: str | None = None
    text: str
    visibility: Visibility
    allowed_groups: list[str] = Field(default_factory=list)
    department: str | None = None
    #: Denormalized from the document so category/class facets stay inside the index query
    #: (ADR-0003). Rewritten whenever the document's classification changes.
    category_path: str
    doc_class: DocClass
    doc_status: DocStatus
    effective_from: date | None = None
    effective_to: date | None = None
    tombstoned: bool = False


class ReviewTask(Base):
    id: UUID
    version_id: UUID
    task_type: ReviewTaskType
    state: ReviewTaskState = ReviewTaskState.OPEN
    assignee_group: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    decision: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


class GraphServingEdge(Base):
    """Read-optimized projection of `document_refs`, rebuilt on publish. Carries the target's
    ACL so graph expansion can be filtered with the same predicate as the main query."""

    src_document_id: UUID
    dst_document_id: UUID
    ref_type: RefType
    dst_canonical_version_id: UUID | None = None
    dst_summary: str | None = None
    dst_visibility: Visibility
    dst_allowed_groups: list[str] = Field(default_factory=list)
    dst_effective_from: date | None = None
    articles: list[int] = Field(default_factory=list)
