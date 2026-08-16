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
    Float,
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
    #: Clause-precise addresses — "12", "12.2", "12.2a". NULL means the whole document.
    #: `articles` is derived from these (ADR-0036).
    anchors: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
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
    #: The article this chunk belongs to, denormalized from the section path by the chunker.
    #: NULL for front matter, an appendix, or a table before Điều 1 — and NULL means the chunk
    #: is flagged only when the *whole* document is, which is the right default: an amendment
    #: to Điều 12 does not put the appendix out of date (ADR-0032).
    article: Mapped[int | None] = mapped_column(Integer)
    #: The full dotted address — "12", "12.2", "12.2a". What a reference resolves against by
    #: equality join; `article` alone cannot tell Khoản 2 from Khoản 3 (ADR-0036).
    anchor: Mapped[str | None] = mapped_column(Text)
    #: The heading chain with instrument boilerplate stripped: what two clauses stating the
    #: same rule share even when worded differently. Consumed by ADR-0033's detection funnel
    #: and ADR-0037's fact sets.
    subject_key: Mapped[str | None] = mapped_column(Text)
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
            "'pii_override','expiry_review','periodic_review')",
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
    #: Kept while the reference waits: an anchor dropped at parking time is one nobody can
    #: recover when the target finally arrives (ADR-0028/0036).
    anchors: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    detected_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentExpiryRow(Base):
    """The expiry ledger: append-only, never updated in place (ADR-0030).

    Expiry is almost always learned *later*, from a *different* instrument, so it cannot live on
    the version — INV-9 makes versions immutable, and the chunks carry a copy of the dates, so
    moving them under a published version makes a past `as_of` query irreproducible.

    Two clocks, kept apart. `effective_to` is when the instrument stopped applying *in the
    world*; `created_at`/`closed_at` are when this platform started and stopped *believing* it.
    Without the second pair the table answers "what was in force on 3 May" but not "what would
    we have answered on 3 May", and an auditor reconstructing a past answer is asking the
    second (INV-11).

    A row carrying `anchors` is a **partial** expiry: the named clauses stop being served and
    the document stays live. Applying one is M9c's (ADR-0040); this table records it from the
    start so the shape never has to change under data.
    """

    __tablename__ = "document_expiry"
    __table_args__ = (
        CheckConstraint(
            "basis IN ('self_stated','abrogated_by','declared_by','steward')",
            name="ck_expiry_basis",
        ),
        CheckConstraint(
            "state IN ('proposed','confirmed','revoked')",
            name="ck_expiry_state",
        ),
        # Only a basis that attributes the decision elsewhere may name a source document. Not
        # the converse: an authorized purge of the abrogating instrument nulls this column.
        CheckConstraint(
            "source_document_id IS NULL OR basis IN ('abrogated_by','declared_by')",
            name="ck_expiry_source_matches_basis",
        ),
        CheckConstraint("source_document_id <> document_id", name="ck_expiry_not_self"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    #: The last day the document (or the named clauses) applied. Inclusive, matching the
    #: effectivity predicate `effective_to >= :effective_on`.
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    #: The instrument that ended it, when there is one.
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    #: Empty/NULL = the whole document. `anchors` is clause-precise ("12", "12.2", "12.2a");
    #: `articles` is derived from it and is what the impact traversal reads (ADR-0036).
    articles: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    anchors: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    #: The sentence the date was read from, so confirming is one glance rather than a document
    #: read. Same discipline as the effective-date review screen (ADR-0029).
    evidence: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    detected_by: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set by the row that replaces this one, in the same transaction. NULL = current belief.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClauseSupersessionRow(Base):
    """One clause replaced by another (ADR-0033/0039/0040).

    Append-only and shaped like `DocumentExpiryRow` on purpose: the expiry ledger says a rule
    ended, this says what took its place, and they are the same kind of record — a decision
    about a rule, made by somebody, on evidence, at a time.

    Anchored on the section path at both ends, never on a chunk id: `_insert_chunks` deletes and
    re-inserts every chunk on each publish and rechunk, so a chunk id here is a dangling pointer
    one rechunk later. The section path is also the only address present on *every* chunk — the
    dotted `anchor` is NULL for front matter, an appendix or a rate-schedule item, all of which
    can be superseded like anything else.
    """

    __tablename__ = "clause_supersessions"
    __table_args__ = (
        CheckConstraint(
            "basis IN ('declared','edge_article','detected','steward')",
            name="ck_clause_sup_basis",
        ),
        CheckConstraint("state IN ('proposed','confirmed','revoked')", name="ck_clause_sup_state"),
        CheckConstraint(
            "(new_document_id IS NULL) = (new_section_path IS NULL)",
            name="ck_clause_sup_replacement_is_whole",
        ),
        CheckConstraint(
            "new_document_id IS NULL OR new_document_id <> old_document_id "
            "OR new_section_path <> old_section_path",
            name="ck_clause_sup_not_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    old_document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    old_section_path: Mapped[str] = mapped_column(Text, nullable=False)
    #: Both NULL together for a pure abrogation: a clause ended and nothing took its place, and
    #: inventing a replacement would be a lie the answer would repeat.
    new_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    new_section_path: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    #: When the replacement took effect. A confirmed supersession is true from the newer
    #: clause's own effective date, not "from now", so an `as_of` query before it still shows
    #: the older clause as current and unflagged (ADR-0033).
    supersedes_from: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL on the declared path, where the corpus stated the answer and nothing was adjudicated.
    verdict: Mapped[str | None] = mapped_column(Text)
    quantity_delta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scope_facets: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    score: Mapped[float | None] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    detected_by: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set by the row that replaces this one. NULL = the current belief.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
