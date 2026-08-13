"""Re-derive the reference graph from what ingest already detected.

Two jobs, both idempotent, both safe to run on a live corpus:

1. **Park** every reference the IDP recorded on a review task, so nothing detected before
   `pending_document_refs` existed is lost.
2. **Promote** the parked references whose target is now in the registry, which is what a
   corpus loaded in arbitrary order needs after the fact (ADR-0028).

Ingest does both automatically from now on; this exists for the documents that arrived before
it did, and for the day a bulk import lands out of order.

The matching key is computed by `normalize_legal_number` and nowhere else. An earlier version
of this script folded case in SQL instead, which silently disagreed with the Python normalizer
about `NĐ-CP` vs `ND-CP` — and a key that disagrees with itself is a graph that never links.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from kb_registry import repository as repo
from kb_registry.service import RegistryService
from kb_schemas.orm import PendingDocumentRefRow
from kb_vntext.legal_numbers import normalize_legal_number
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

DETECTED_SQL = """
SELECT v.document_id, ref->>'legal_number' AS legal_number, ref->>'ref_type' AS ref_type
FROM review_tasks t
JOIN document_versions v ON v.id = t.version_id
CROSS JOIN LATERAL jsonb_array_elements(t.payload->'detected_refs') AS ref
WHERE t.payload ? 'detected_refs' AND ref->>'legal_number' IS NOT NULL
"""


def main() -> None:
    configure_logging("kb-relink", "INFO", "console")
    engine = create_db_engine()
    with Session(engine) as session:
        # Bring any pre-existing rows onto the current key, so a row parked by an older path
        # can still be found by the arrival lookup.
        rekeyed = 0
        for row in session.execute(
            text("SELECT id, target_legal_number, target_key FROM pending_document_refs")
        ).mappings():
            key = normalize_legal_number(row["target_legal_number"])
            if key != row["target_key"]:
                session.execute(
                    text("UPDATE pending_document_refs SET target_key = :key WHERE id = :id"),
                    {"key": key, "id": row["id"]},
                )
                rekeyed += 1

        parked = 0
        for row in session.execute(text(DETECTED_SQL)).mappings():
            before = session.execute(text("SELECT count(*) FROM pending_document_refs")).scalar()
            repo.add_pending_ref(
                session,
                PendingDocumentRefRow(
                    id=uuid.uuid4(),
                    src_document_id=row["document_id"],
                    target_legal_number=row["legal_number"],
                    target_key=normalize_legal_number(row["legal_number"]),
                    ref_type=row["ref_type"],
                    articles=None,
                    detected_by="idp",
                    created_at=datetime.now(UTC),
                ),
            )
            after = session.execute(text("SELECT count(*) FROM pending_document_refs")).scalar()
            parked += int((after or 0) > (before or 0))

        registry = RegistryService(session)
        document_ids = [
            row[0]
            for row in session.execute(
                text("SELECT id FROM documents WHERE legal_number IS NOT NULL ORDER BY created_at")
            )
        ]
        promoted = sum(
            len(registry.resolve_pending_refs(document_id)) for document_id in document_ids
        )
        waiting = session.execute(text("SELECT count(*) FROM pending_document_refs")).scalar() or 0
        session.commit()

    print(f"re-keyed {rekeyed} rows onto the current normalizer")
    print(f"parked {parked} newly recorded references")
    print(f"promoted {promoted} into edges")
    print(f"{waiting} still waiting for a document the registry does not hold")


if __name__ == "__main__":
    main()
