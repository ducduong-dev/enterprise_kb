"""Re-read the corpus's references for the clauses they name.

`document_refs.anchors` (migration 0008) is written by ingest from now on. Every edge detected
before that has NULL anchors, which means "the whole document" — today's behaviour, so nothing
is broken, but a reference to *khoản 2 Điều 12* resolves to all of Điều 12 until this runs.

Unlike the chunk columns, this cannot be derived from what is already in the registry: an
anchor lives in the *citing* document's sentence, and the edge records only its endpoints. So
this re-reads the stored KBDoc of each citing document and re-runs the detector over it —
which is why it is a script rather than a migration, and why it is safe to run repeatedly on a
live corpus.

Three things it will not do:

* **It never creates or removes an edge.** Only anchors on edges that already exist, plus the
  `articles` derived from them. Re-detection can disagree with a human who confirmed an edge,
  and this is not the place to relitigate that (M5's review screen is).
* **It never overwrites a non-empty anchor list**, unless `--force`. A steward may have
  corrected one on the inspection screen, and a nightly re-read that silently reverts human
  corrections is worse than no backfill.
* **It leaves an edge alone when the citing document's KBDoc is gone.** An archived derived
  file is not an error; the edge keeps article granularity, which is where it started.
"""

from __future__ import annotations

import argparse
import json

from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_ports.adapters.storage_s3 import S3StorageAdapter
from kb_ports.storage import StoragePort
from kb_schemas.kbdoc import KBDoc
from kb_vntext.legal_numbers import extract_legal_numbers, find_anchors, normalize_legal_number
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Citing documents whose edges we might improve: everything with a canonical version whose
#: IDP report was kept.
_CITING = """
    SELECT DISTINCT r.src_document_id AS document_id, v.idp_report_ref
    FROM document_refs r
    JOIN documents d ON d.id = r.src_document_id
    JOIN document_versions v ON v.id = d.canonical_version_id
    WHERE v.idp_report_ref IS NOT NULL
    ORDER BY r.src_document_id
"""


def _storage() -> StoragePort:
    from kb_common.config import get_settings

    settings = get_settings()
    if settings.env == "test":
        return LocalStorageAdapter("/tmp/kb-storage")
    return S3StorageAdapter(settings.storage)


def _anchors_by_target(kbdoc: KBDoc) -> dict[str, list[str]]:
    """Every instrument this document cites, and the clauses it names of each.

    Keyed by the normalized legal number, which is what the edge's endpoint resolves through —
    a key computed any other way is a backfill that silently matches nothing (the lesson
    `relink_references.py` records).
    """
    found: dict[str, list[str]] = {}
    for block in kbdoc.blocks:
        if not block.text:
            continue
        for number in extract_legal_numbers(block.text):
            anchors = find_anchors(block.text, number.start)
            if not anchors:
                continue
            key = normalize_legal_number(number.value)
            for anchor in anchors:
                if anchor not in found.setdefault(key, []):
                    found[key].append(anchor)
    return found


def backfill(session: Session, storage: StoragePort, *, force: bool = False) -> tuple[int, int]:
    """Returns (documents read, edges updated)."""
    read = 0
    updated = 0

    for row in session.execute(text(_CITING)).all():
        bucket, _, key = str(row.idp_report_ref).partition("/")
        try:
            body = storage.get(bucket, key)
        except Exception as exc:  # pragma: no cover - an archived derived file is normal
            log.info(
                "kbdoc_unavailable",
                extra={"document_id": str(row.document_id), "error": str(exc)},
            )
            continue

        kbdoc = KBDoc.model_validate(json.loads(body.decode("utf-8")))
        read += 1
        by_target = _anchors_by_target(kbdoc)
        if not by_target:
            continue

        edges = session.execute(
            text(
                """
                SELECT r.id, r.anchors, d.legal_number
                FROM document_refs r
                JOIN documents d ON d.id = r.dst_document_id
                WHERE r.src_document_id = :src AND d.legal_number IS NOT NULL
                """
            ),
            {"src": row.document_id},
        ).all()

        for edge in edges:
            if edge.anchors and not force:
                continue
            anchors = by_target.get(normalize_legal_number(str(edge.legal_number)))
            if not anchors:
                continue
            articles = sorted({int(a.split(".")[0]) for a in anchors if a.split(".")[0].isdigit()})
            session.execute(
                text(
                    "UPDATE document_refs SET anchors = CAST(:anchors AS TEXT[]), "
                    "articles = CAST(:articles AS INT[]) WHERE id = :id"
                ),
                {"anchors": anchors, "articles": articles or None, "id": edge.id},
            )
            updated += 1
        session.commit()

    return read, updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite anchors that are already set, including a steward's corrections",
    )
    args = parser.parse_args()

    configure_logging("backfill-ref-anchors", "INFO", "text")
    with Session(create_db_engine()) as session:
        read, updated = backfill(session, _storage(), force=args.force)
    print(f"read {read} citing documents")
    print(f"gave anchors to {updated} edges")
    print("edges whose citing text is unavailable keep article granularity")


if __name__ == "__main__":
    main()
