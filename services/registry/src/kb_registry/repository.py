"""Data access for the registry tables.

Plain functions over a caller-supplied `Session`: the publish transaction (M2) needs several
of these to run inside one transaction it controls, so nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from kb_common.db import affected_rows
from kb_schemas.orm import (
    CategoryRow,
    DocumentDeclarationRow,
    DocumentRefRow,
    DocumentRow,
    DocumentVersionRow,
    OutboxRow,
    PendingDocumentRefRow,
    ReviewTaskRow,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session


def now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------ categories


def get_category(session: Session, path: str) -> CategoryRow | None:
    return session.get(CategoryRow, path)


def list_categories(session: Session, prefix: str | None = None) -> Sequence[CategoryRow]:
    statement = select(CategoryRow).order_by(CategoryRow.path)
    rows = session.execute(statement).scalars().all()
    if prefix:
        rows = [row for row in rows if row.path == prefix or row.path.startswith(f"{prefix}.")]
    return rows


def add_category(session: Session, row: CategoryRow) -> CategoryRow:
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------------------- documents


def get_document(session: Session, document_id: uuid.UUID) -> DocumentRow | None:
    return session.get(DocumentRow, document_id)


def find_by_legal_number(session: Session, legal_number: str) -> DocumentRow | None:
    """Exact match only.

    M1 deliberately stops here: fuzzy matching is M5's identity resolution, with its own
    thresholds and a human review step. An exact hit is the one case that needs no judgment.
    """
    return session.execute(
        select(DocumentRow).where(DocumentRow.legal_number == legal_number)
    ).scalar_one_or_none()


def list_documents(
    session: Session,
    *,
    category_prefix: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    acl: tuple[str, dict[str, Any]] | None = None,
) -> Sequence[DocumentRow]:
    """Registry rows, newest first.

    `acl` is the caller's compiled visibility predicate — `compile_sql_documents(filter,
    alias="documents")`, whose alias must be the table's own name because this is an ORM
    select rather than a hand-written FROM. It is an argument rather than a default because
    the workflows and the seeder list documents as the platform itself; every *caller-facing*
    listing passes one, and without it the row itself discloses that the document exists.
    """
    statement = select(DocumentRow).order_by(DocumentRow.updated_at.desc())
    if status:
        statement = statement.where(DocumentRow.status == status)
    if acl is not None:
        fragment, params = acl
        statement = statement.where(text(fragment).bindparams(**params))
    rows = list(session.execute(statement).scalars().all())
    if category_prefix:
        rows = [
            row
            for row in rows
            if row.category_path == category_prefix
            or row.category_path.startswith(f"{category_prefix}.")
        ]
    return rows[offset : offset + limit]


def add_document(session: Session, row: DocumentRow) -> DocumentRow:
    session.add(row)
    session.flush()
    return row


# -------------------------------------------------------------------------------- versions


def get_version(session: Session, version_id: uuid.UUID) -> DocumentVersionRow | None:
    return session.get(DocumentVersionRow, version_id)


def list_versions(session: Session, document_id: uuid.UUID) -> Sequence[DocumentVersionRow]:
    return (
        session.execute(
            select(DocumentVersionRow)
            .where(DocumentVersionRow.document_id == document_id)
            .order_by(DocumentVersionRow.created_at.desc())
        )
        .scalars()
        .all()
    )


def get_canonical_version(session: Session, document_id: uuid.UUID) -> DocumentVersionRow | None:
    return session.execute(
        select(DocumentVersionRow).where(
            DocumentVersionRow.document_id == document_id,
            DocumentVersionRow.is_canonical.is_(True),
        )
    ).scalar_one_or_none()


def find_version_by_hash(
    session: Session, document_id: uuid.UUID, content_hash: str
) -> DocumentVersionRow | None:
    """Re-uploading identical bytes must not create a second version of the same content."""
    return session.execute(
        select(DocumentVersionRow).where(
            DocumentVersionRow.document_id == document_id,
            DocumentVersionRow.content_hash == content_hash,
        )
    ).scalar_one_or_none()


def add_version(session: Session, row: DocumentVersionRow) -> DocumentVersionRow:
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------------------------- edges


def get_edge(
    session: Session, src: uuid.UUID, dst: uuid.UUID, ref_type: str
) -> DocumentRefRow | None:
    return session.execute(
        select(DocumentRefRow).where(
            DocumentRefRow.src_document_id == src,
            DocumentRefRow.dst_document_id == dst,
            DocumentRefRow.ref_type == ref_type,
        )
    ).scalar_one_or_none()


def add_edge(session: Session, row: DocumentRefRow) -> DocumentRefRow:
    session.add(row)
    session.flush()
    return row


def list_edges_from(session: Session, document_id: uuid.UUID) -> Sequence[DocumentRefRow]:
    return (
        session.execute(select(DocumentRefRow).where(DocumentRefRow.src_document_id == document_id))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------- review tasks


def add_review_task(session: Session, row: ReviewTaskRow) -> ReviewTaskRow:
    session.add(row)
    session.flush()
    return row


def get_review_task(session: Session, task_id: uuid.UUID) -> ReviewTaskRow | None:
    return session.get(ReviewTaskRow, task_id)


def list_review_tasks(
    session: Session,
    *,
    assignee_groups: Sequence[str] | None = None,
    state: str | None = "open",
    task_type: str | None = None,
    limit: int = 50,
) -> Sequence[ReviewTaskRow]:
    statement = select(ReviewTaskRow).order_by(ReviewTaskRow.created_at)
    if state:
        statement = statement.where(ReviewTaskRow.state == state)
    if task_type:
        statement = statement.where(ReviewTaskRow.task_type == task_type)
    if assignee_groups is not None:
        # A queue is visible to the group that owns it; an empty list means no queues.
        statement = statement.where(ReviewTaskRow.assignee_group.in_(list(assignee_groups)))
    return session.execute(statement.limit(limit)).scalars().all()


# ---------------------------------------------------------------------------------- outbox


def enqueue(session: Session, topic: str, payload: dict[str, Any]) -> OutboxRow:
    """Append to the transactional outbox.

    Always called inside the caller's transaction: an event that commits without its state
    change (or the reverse) is the divergence INV-5 exists to prevent.
    """
    row = OutboxRow(ts=now(), topic=topic, payload=payload)
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------- pending references


def add_pending_ref(session: Session, row: PendingDocumentRefRow) -> PendingDocumentRefRow:
    """Park a reference whose target is not registered yet.

    Idempotent by (source, target, type): re-ingesting the same document must not accumulate
    duplicate waiting rows.
    """
    session.execute(
        text(
            """
            INSERT INTO pending_document_refs (id, src_document_id, target_legal_number,
                target_key, ref_type, articles, anchors, detected_by, created_at)
            VALUES (:id, :src, :number, :key, CAST(:ref_type AS ref_type),
                CAST(:articles AS INT[]), CAST(:anchors AS TEXT[]), :detected_by, :created_at)
            ON CONFLICT ON CONSTRAINT uq_pending_ref DO NOTHING
            """
        ),
        {
            "id": row.id,
            "src": row.src_document_id,
            "number": row.target_legal_number,
            "key": row.target_key,
            "ref_type": row.ref_type,
            "articles": list(row.articles) if row.articles else None,
            "anchors": list(row.anchors) if row.anchors else None,
            "detected_by": row.detected_by,
            "created_at": row.created_at,
        },
    )
    return row


def add_declaration(session: Session, row: DocumentDeclarationRow) -> bool:
    """Store a read declaration. Returns False when the same one is already recorded.

    Idempotent on `(src, target_key, kind, anchors)`: re-ingesting a document must not
    accumulate duplicate readings of the same sentence. Anchors are in the key because one
    closing article legitimately declares several changes against the same instrument.
    """
    result = session.execute(
        text(
            """
            INSERT INTO document_declarations (id, src_document_id, kind, target_legal_number,
                target_key, target_document_id, target_anchors, replacement_anchors,
                effective_from, evidence, block_id, confidence, state, detected_by, created_at)
            VALUES (:id, :src, :kind, :number, :key, :target, CAST(:target_anchors AS TEXT[]),
                CAST(:replacement_anchors AS TEXT[]), :effective_from, :evidence, :block_id,
                :confidence, :state, :detected_by, :created_at)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": row.id,
            "src": row.src_document_id,
            "kind": row.kind,
            "number": row.target_legal_number,
            "key": row.target_key,
            "target": row.target_document_id,
            "target_anchors": list(row.target_anchors) if row.target_anchors else None,
            "replacement_anchors": (
                list(row.replacement_anchors) if row.replacement_anchors else None
            ),
            "effective_from": row.effective_from,
            "evidence": row.evidence,
            "block_id": row.block_id,
            "confidence": row.confidence,
            "state": row.state,
            "detected_by": row.detected_by,
            "created_at": row.created_at,
        },
    )
    return affected_rows(result) > 0


def waiting_declarations_for(session: Session, target_key: str) -> Sequence[DocumentDeclarationRow]:
    """Declarations parked against a legal number the registry did not hold."""
    return (
        session.execute(
            select(DocumentDeclarationRow).where(
                DocumentDeclarationRow.target_key == target_key,
                DocumentDeclarationRow.target_document_id.is_(None),
            )
        )
        .scalars()
        .all()
    )


def declarations_from(session: Session, document_id: uuid.UUID) -> Sequence[DocumentDeclarationRow]:
    """Everything this document declares, for the batch review screen."""
    return (
        session.execute(
            select(DocumentDeclarationRow)
            .where(DocumentDeclarationRow.src_document_id == document_id)
            .order_by(DocumentDeclarationRow.created_at, DocumentDeclarationRow.id)
        )
        .scalars()
        .all()
    )


def pending_refs_for(session: Session, target_key: str) -> Sequence[PendingDocumentRefRow]:
    """Every parked reference naming this legal number."""
    return (
        session.execute(
            select(PendingDocumentRefRow).where(PendingDocumentRefRow.target_key == target_key)
        )
        .scalars()
        .all()
    )


def pending_refs_from(session: Session, document_id: uuid.UUID) -> Sequence[PendingDocumentRefRow]:
    """What this document points at that the bank does not hold — the corpus's own to-do list."""
    return (
        session.execute(
            select(PendingDocumentRefRow).where(
                PendingDocumentRefRow.src_document_id == document_id
            )
        )
        .scalars()
        .all()
    )


def drop_pending_ref(session: Session, row: PendingDocumentRefRow) -> None:
    session.execute(text("DELETE FROM pending_document_refs WHERE id = :id"), {"id": row.id})


def refresh_graph_serving_for(session: Session, document_id: uuid.UUID) -> int:
    """Rebuild the serving projection for every edge touching this document.

    The projection carries the *target's* ACL so graph expansion can be filtered with the same
    predicate as the main query (INV-10). One implementation, called from the publish
    transaction and from the moment a late-arriving document turns a parked reference into an
    edge — two places that must not drift.
    """
    rows = (
        session.execute(
            text(
                """
                SELECT r.src_document_id, r.dst_document_id, r.ref_type, r.articles,
                       d.canonical_version_id, d.title, d.visibility, d.allowed_groups,
                       v.effective_from
                FROM document_refs r
                JOIN documents d ON d.id = r.dst_document_id
                LEFT JOIN document_versions v ON v.id = d.canonical_version_id
                WHERE r.src_document_id = :id OR r.dst_document_id = :id
                """
            ),
            {"id": document_id},
        )
        .mappings()
        .all()
    )
    for row in rows:
        session.execute(
            text(
                """
                INSERT INTO graph_serving (src_document_id, dst_document_id, ref_type,
                    dst_canonical_version_id, dst_summary, dst_visibility,
                    dst_allowed_groups, dst_effective_from, articles)
                VALUES (:src, :dst, CAST(:ref_type AS ref_type), :canonical, :summary,
                    CAST(:visibility AS visibility), CAST(:groups AS TEXT[]),
                    :effective_from, CAST(:articles AS INT[]))
                ON CONFLICT (src_document_id, dst_document_id, ref_type) DO UPDATE SET
                    dst_canonical_version_id = EXCLUDED.dst_canonical_version_id,
                    dst_summary = EXCLUDED.dst_summary,
                    dst_visibility = EXCLUDED.dst_visibility,
                    dst_allowed_groups = EXCLUDED.dst_allowed_groups,
                    dst_effective_from = EXCLUDED.dst_effective_from,
                    articles = EXCLUDED.articles
                """
            ),
            {
                "src": row["src_document_id"],
                "dst": row["dst_document_id"],
                "ref_type": row["ref_type"],
                "canonical": row["canonical_version_id"],
                # M5 replaces this with an LLM summary; the title is what a reader needs in
                # order to decide whether to follow the reference.
                "summary": row["title"],
                "visibility": row["visibility"],
                "groups": list(row["allowed_groups"] or []),
                "effective_from": row["effective_from"],
                "articles": list(row["articles"]) if row["articles"] else None,
            },
        )
    session.flush()
    return len(rows)
