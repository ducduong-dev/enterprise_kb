"""Registry request/response models.

Note what is *not* here: no field lets a caller set `is_canonical`, `canonical_version_id` or
`status='published'`. Publication happens only through the publish transaction (M2, INV-5), so
the CRUD surface cannot be used to route around it.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from kb_schemas.enums import DocClass, DocStatus, PiiStatus, RefType, SourceType, Visibility
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: str
    default_visibility: Visibility = Visibility.INTERNAL_ALL
    default_allowed_groups: list[str] = Field(default_factory=list)
    steward_group: str | None = None
    existence_disclosure: bool = False


class DocumentCreate(BaseModel):
    """Classification supplied by the uploader. Anything omitted falls back to the category
    default, which is why the category tree is the real ACL policy surface."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    doc_class: DocClass
    category_path: str
    legal_number: str | None = None
    department: str | None = None
    visibility: Visibility | None = None
    allowed_groups: list[str] | None = None
    review_by: date | None = None

    @model_validator(mode="after")
    def _restricted_needs_groups(self) -> DocumentCreate:
        if self.visibility is Visibility.RESTRICTED and not self.allowed_groups:
            raise ValueError("restricted documents must name at least one allowed group")
        return self


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    department: str | None = None
    visibility: Visibility | None = None
    allowed_groups: list[str] | None = None
    category_path: str | None = None
    review_by: date | None = None


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    content_ref: str
    content_hash: str
    source_type: SourceType = SourceType.UPLOAD
    author: str
    change_summary: str | None = None
    idp_report_ref: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    legal_number: str | None
    doc_class: DocClass
    category_path: str
    department: str | None
    visibility: Visibility
    allowed_groups: list[str]
    status: DocStatus
    canonical_version_id: UUID | None
    review_by: date | None
    created_at: datetime
    updated_at: datetime


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    content_ref: str | None
    content_hash: str | None
    source_type: str
    author: str | None
    change_summary: str | None
    idp_report_ref: str | None
    pii_status: PiiStatus
    effective_from: date | None
    effective_to: date | None
    is_canonical: bool
    retention_until: date | None
    created_at: datetime


class DetectedRefIn(BaseModel):
    """A reference the IDP proposed. `confirmed_by` stays empty until a human agrees (M5)."""

    model_config = ConfigDict(extra="forbid")

    legal_number: str
    ref_type: RefType = RefType.CITES
    #: Clause-precise, in the dotted form the target's chunks carry. `articles` is derived from
    #: it rather than supplied, so the two cannot disagree (ADR-0036).
    anchors: list[str] = Field(default_factory=list)
    articles: list[int] = Field(default_factory=list)
    detected_by: str = "idp"


class RefLinkResult(BaseModel):
    created: list[UUID] = Field(default_factory=list)
    #: Mentions whose target is not in the registry yet. Kept for the review task rather than
    #: dropped: "this cites something we do not have" is exactly what a steward needs to see.
    unresolved: list[str] = Field(default_factory=list)


class ReviewTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version_id: UUID
    task_type: str
    state: str
    assignee_group: str | None
    payload: dict[str, object]
    decision: str | None
    decided_by: str | None
    decided_at: datetime | None
