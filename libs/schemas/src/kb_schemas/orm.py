"""SQLAlchemy mapping of the section-4 schema.

The migration in `ops/alembic/versions/` is authoritative; this mapping must match it. CI runs
`alembic check` against a live database so drift fails the build rather than surfacing as a
runtime error in production.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    types,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kb_schemas.enums import DocClass, DocStatus, PiiStatus, RefType, Visibility

EMBEDDING_DIM = 1024


class LTREE(types.UserDefinedType[str]):
    """Minimal `ltree` binding — we only ever read/write it as text and use `<@` in raw SQL."""

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "LTREE"


class Base(DeclarativeBase):
    pass


def _enum(python_enum: type, name: str) -> ENUM:
    # create_type=False: the migration owns the type, the mapping only references it.
    return ENUM(
        *[member.value for member in python_enum],  # type: ignore[attr-defined]
        name=name,
        create_type=False,
    )


visibility_enum = _enum(Visibility, "visibility")
doc_status_enum = _enum(DocStatus, "doc_status")
doc_class_enum = _enum(DocClass, "doc_class")
ref_type_enum = _enum(RefType, "ref_type")


class CategoryRow(Base):
    __tablename__ = "categories"

    path: Mapped[str] = mapped_column(LTREE, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    default_visibility: Mapped[str] = mapped_column(
        visibility_enum, nullable=False, default=Visibility.INTERNAL_ALL.value
    )
    default_allowed_groups: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    steward_group: Mapped[str | None] = mapped_column(Text)
    existence_disclosure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "visibility <> 'restricted' OR coalesce(array_length(allowed_groups, 1), 0) >= 1",
            name="ck_documents_restricted_needs_groups",
        ),
        # Deferrable: publish flips the document pointer and the version flag in one
        # transaction, in either order (INV-5).
        ForeignKeyConstraint(
            ["canonical_version_id"],
            ["document_versions.id"],
            name="fk_documents_canonical_version",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: UNIQUE NULLS NOT DISTINCT — at most one document per legal number, and at most one
    #: document without one is *not* implied: NULLS NOT DISTINCT would collapse them, so the
    #: migration applies the constraint only to non-null values via a partial unique index.
    legal_number: Mapped[str | None] = mapped_column(Text)
    doc_class: Mapped[str] = mapped_column(doc_class_enum, nullable=False)
    category_path: Mapped[str] = mapped_column(
        LTREE, ForeignKey("categories.path", onupdate="CASCADE"), nullable=False
    )
    department: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(visibility_enum, nullable=False)
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    status: Mapped[str] = mapped_column(doc_status_enum, nullable=False)
    canonical_version_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    review_by: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentVersionRow(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint(
            "pii_status IN ('pending','clear','blocked','overridden')",
            name="ck_versions_pii_status",
        ),
        CheckConstraint(
            "source_type IN ('upload','portal_edit','consolidation')",
            name="ck_versions_source_type",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="ck_versions_effective_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    content_ref: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text)
    change_summary: Mapped[str | None] = mapped_column(Text)
    idp_report_ref: Mapped[str | None] = mapped_column(Text)
    pii_status: Mapped[str] = mapped_column(Text, nullable=False, default=PiiStatus.PENDING.value)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    is_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When this version became canonical. Publish-to-searchable latency is measured from it.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentRefRow(Base):
    __tablename__ = "document_refs"
    __table_args__ = (
        CheckConstraint("src_document_id <> dst_document_id", name="ck_refs_no_self_edge"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    src_document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    dst_document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    ref_type: Mapped[str] = mapped_column(ref_type_enum, nullable=False)
    articles: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    detected_by: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChunkRow(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint(
            "visibility <> 'restricted' OR coalesce(array_length(allowed_groups, 1), 0) >= 1",
            name="ck_chunks_restricted_needs_groups",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=False
    )
    section_path: Mapped[str | None] = mapped_column(Text)
    citation_label: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # Denormalized ACL — lets the filter live in the WHERE clause (INV-2), no join needed.
    visibility: Mapped[str] = mapped_column(visibility_enum, nullable=False)
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    department: Mapped[str | None] = mapped_column(Text)
    # Denormalized facet columns (ADR-0003): the category and class facets must be applied
    # inside the index query, and the keyword index is chunk-shaped with no join available.
    category_path: Mapped[str] = mapped_column(LTREE, nullable=False)
    doc_class: Mapped[str] = mapped_column(doc_class_enum, nullable=False)
    doc_status: Mapped[str] = mapped_column(doc_status_enum, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    tombstoned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Position within the version, so results can be read back in document order.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Source page — the review editor (M3) jumps from a chunk to its page image.
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ReviewTaskRow(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('idp_review','identity_review','merge_review','impact_review',"
            "'pii_override')",
            name="ck_tasks_type",
        ),
        CheckConstraint("state IN ('open','claimed','decided','cancelled')", name="ck_tasks_state"),
        # Four-eyes and PII overrides are only meaningful with an attributable decider.
        CheckConstraint(
            "state <> 'decided' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_tasks_decided_attribution",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    assignee_group: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    decision: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PendingDocumentRefRow(Base):
    """A detected reference whose target is not in the registry yet.

    It waits here rather than in `document_refs`, because an edge with one endpoint is not an
    edge and every consumer of the graph would have to remember to exclude it (INV-10). When a
    document with this legal number is registered, the row becomes a real edge and is deleted.
    """

    __tablename__ = "pending_document_refs"
    __table_args__ = (
        UniqueConstraint("src_document_id", "target_key", "ref_type", name="uq_pending_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    src_document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    #: As detected, for display.
    target_legal_number: Mapped[str] = mapped_column(Text, nullable=False)
    #: `normalize_legal_number` of the above: what the arrival lookup matches on.
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    ref_type: Mapped[str] = mapped_column(ref_type_enum, nullable=False)
    articles: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    detected_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GraphServingRow(Base):
    __tablename__ = "graph_serving"

    src_document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    dst_document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    ref_type: Mapped[str] = mapped_column(ref_type_enum, primary_key=True)
    dst_canonical_version_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    dst_summary: Mapped[str | None] = mapped_column(Text)
    dst_visibility: Mapped[str] = mapped_column(visibility_enum, nullable=False)
    dst_allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    dst_effective_from: Mapped[date | None] = mapped_column(Date)
    articles: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    on_behalf_of: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_filter: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class OutboxRow(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


metadata = Base.metadata
