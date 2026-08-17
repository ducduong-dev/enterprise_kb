"""Minimal registry rows, built with SQL rather than through the services.

Deliberately not `RegistryService.create_document` and friends: these exist for tests about
what happens *to* a published document — expiry, the sweep — where going through ingest,
chunking and the publish transaction would make the test about the pipeline instead. Anything
testing the pipeline itself should use the real path.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

#: A real seeded category, so the steward-group lookup resolves the way it does in production.
DEFAULT_CATEGORY = "internal.procedures"
#: `chunks.embedding` is `VECTOR(1024)` and NOT NULL-ish in practice; the value never matters
#: to these tests, only that the row exists and carries dates.
_ZERO_VECTOR = "[" + ",".join(["0.0"] * 1024) + "]"


def make_document(
    session: Session,
    *,
    title: str = "Thông tư thử nghiệm",
    category: str = DEFAULT_CATEGORY,
    status: str = "published",
    doc_class: str = "operational",
    legal_number: str | None = None,
) -> uuid.UUID:
    """`legal_number` is optional because most callers do not care; M9d's funnel does, because
    the instrument code in it is what breaks a same-day tie between two clauses."""
    doc_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO documents (id, title, legal_number, doc_class, category_path, visibility,
                                   allowed_groups, status, created_at, updated_at)
            VALUES (:id, :title, :legal_number, CAST(:doc_class AS doc_class),
                    CAST(:cat AS ltree), 'internal_all', '{}', CAST(:status AS doc_status),
                    now(), now())
            """
        ),
        {
            "id": doc_id,
            "title": title,
            "legal_number": legal_number,
            "cat": category,
            "status": status,
            "doc_class": doc_class,
        },
    )
    return doc_id


def make_version(
    session: Session,
    document_id: uuid.UUID,
    *,
    effective_from: date | None = None,
    effective_to: date | None = None,
    canonical: bool = True,
) -> uuid.UUID:
    version_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO document_versions (id, document_id, content_ref, content_hash,
                                           source_type, author, pii_status, is_canonical,
                                           effective_from, effective_to, retention_until,
                                           created_at, published_at)
            VALUES (:id, :doc, 'kb-originals/x', :hash, 'upload', 'tester', 'clear', :canonical,
                    :eff_from, :eff_to, :retention, now(), now())
            """
        ),
        {
            "id": version_id,
            "doc": document_id,
            "hash": uuid.uuid4().hex,
            "canonical": canonical,
            "eff_from": effective_from,
            "eff_to": effective_to,
            "retention": date(2036, 12, 31),
        },
    )
    if canonical:
        session.execute(
            text("UPDATE documents SET canonical_version_id = :v WHERE id = :d"),
            {"v": version_id, "d": document_id},
        )
    return version_id


def make_chunk(
    session: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    effective_to: date | None = None,
    effective_from: date | None = None,
    tombstoned: bool = False,
    category: str = DEFAULT_CATEGORY,
    ordinal: int = 0,
    section_path: str = "Điều 1",
    body: str = "Nội dung thử nghiệm.",
    subject_key: str | None = None,
    embedding: str | None = None,
) -> uuid.UUID:
    """The last four arguments exist for M9d's funnel, which pairs clauses by what they say.

    Defaults reproduce exactly what this helper produced before they were added, so every
    existing caller is unaffected. `subject_key` and `embedding` are the two channels gate 1
    searches on — a chunk with neither is invisible to candidate generation, which is the right
    default for tests that are not about it.
    """
    chunk_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO chunks (id, document_id, version_id, section_path, citation_label, text,
                                subject_key, embedding, visibility, allowed_groups, department,
                                category_path, doc_class, doc_status, effective_from,
                                effective_to, tombstoned, ordinal, page)
            VALUES (:id, :doc, :ver, :section_path, :section_path, :body,
                    :subject_key, CAST(:embedding AS vector), 'internal_all', '{}', NULL,
                    CAST(:cat AS ltree), 'operational', 'published', :eff_from, :eff_to,
                    :tombstoned, :ordinal, 1)
            """
        ),
        {
            "id": chunk_id,
            "doc": document_id,
            "ver": version_id,
            "section_path": section_path,
            "body": body,
            "subject_key": subject_key,
            "embedding": embedding or _ZERO_VECTOR,
            "cat": category,
            "eff_from": effective_from,
            "eff_to": effective_to,
            "tombstoned": tombstoned,
            "ordinal": ordinal,
        },
    )
    return chunk_id


def chunk_dates(session: Session, document_id: uuid.UUID) -> list[date | None]:
    """The `effective_to` the serving path actually reads, for the chunks it can reach."""
    rows = session.execute(
        text(
            "SELECT effective_to FROM chunks WHERE document_id = :d AND NOT tombstoned "
            "ORDER BY ordinal"
        ),
        {"d": document_id},
    )
    return [row[0] for row in rows]


__all__ = ["DEFAULT_CATEGORY", "chunk_dates", "make_chunk", "make_document", "make_version"]
