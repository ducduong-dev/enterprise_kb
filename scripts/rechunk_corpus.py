"""Rebuild derived chunk data across the corpus, one document at a time.

Written for the `subject_key` repair (`ad8b695`), and useful whenever the chunker or the
embedding model improves. `scripts/backfill_chunk_article.py` handles the columns that are pure
functions of `section_path` and cannot touch this one: `subject_key` is built from the heading
*titles*, which live only in the stored KBDoc, so filling it means re-running the chunker.

**This is not a publish.** Nothing about what the bank says changes: same document, same
canonical version, same text, same PII verdict, no four-eyes gate. What changes is the derived
form. `PublishService.rechunk` is the one entry point and it keeps INV-5 — each document's
chunks are replaced in a single transaction, so a reader never sees them half-swapped — and
writes an audit record, because retrieval's answers move and somebody will ask why.

**Chunk ids do not survive**, by design and not by accident: `_insert_chunks` deletes and
re-inserts, which is why no M9 table keys on them. Two consequences worth knowing before
running this:

* `audit_log` retrieve records from before the run keep chunk ids that now resolve to nothing.
  `kb_registry.demand` already counts those as `unresolved_retrievals` and says so rather than
  reporting demand as zero — that is the number to expect to move.
* Everything anchored on `(document_id, section_path)` is unaffected. That is the whole reason
  the supersession, declaration and expiry records are addressed that way.

**Embeddings are recomputed**, so this costs one embedding call per chunk against whatever
adapter is configured. Check `--dry-run` first: it reports the work without doing any of it.

    python scripts/rechunk_corpus.py --dry-run
    python scripts/rechunk_corpus.py --missing-subject-key
    python scripts/rechunk_corpus.py --document <id>
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass

from kb_common.audit import SqlAuditSink
from kb_common.config import get_settings
from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.embedding_tei import TeiEmbeddingAdapter
from kb_ports.adapters.storage_s3 import S3StorageAdapter
from kb_ports.models import EmbeddingPort
from kb_ports.storage import StoragePort
from kb_registry.publish import PublishService
from kb_schemas.kbdoc import KBDoc
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Documents that could be rechunked: published, canonical, and holding the derived KBDoc the
#: chunker needs. A seeded fixture has no `idp_report_ref` and is written directly by
#: `scripts/seed.py`, so it is neither a candidate nor a problem.
_CANDIDATES = """
    SELECT d.id, d.title, v.idp_report_ref,
           count(c.id) FILTER (WHERE NOT c.tombstoned) AS chunks,
           count(c.subject_key) FILTER (WHERE NOT c.tombstoned) AS keyed
    FROM documents d
    JOIN document_versions v ON v.id = d.canonical_version_id
    LEFT JOIN chunks c ON c.document_id = d.id
    WHERE d.status = 'published'
    GROUP BY d.id, d.title, v.idp_report_ref
    ORDER BY chunks DESC
"""


@dataclass(frozen=True, slots=True)
class Candidate:
    document_id: uuid.UUID
    title: str
    report_ref: str | None
    chunks: int
    keyed: int

    @property
    def rechunkable(self) -> bool:
        return bool(self.report_ref)

    @property
    def needs_subject_key(self) -> bool:
        """Live chunks exist and none of them carries a subject key.

        Deliberately "none" rather than "some": a document where only front matter lacks a key
        is correct, because a heading with no title has no subject to record.
        """
        return self.chunks > 0 and self.keyed == 0


def candidates(session: Session) -> list[Candidate]:
    return [
        Candidate(
            document_id=row.id,
            title=row.title,
            report_ref=row.idp_report_ref,
            chunks=int(row.chunks),
            keyed=int(row.keyed),
        )
        for row in session.execute(text(_CANDIDATES))
    ]


def build_embedder() -> EmbeddingPort:
    settings = get_settings()
    # Same choice portal-api makes. The deterministic adapter declares itself as such, so an
    # accidental run against it is visible in the adapter info rather than silent.
    return HashedEmbeddingAdapter() if settings.use_deterministic_models else TeiEmbeddingAdapter()


def load_kbdoc(storage: StoragePort, ref: str) -> KBDoc:
    bucket, _, key = ref.partition("/")
    return KBDoc.model_validate_json(storage.get(bucket, key))


def rechunk_one(
    session: Session,
    storage: StoragePort,
    embedder: EmbeddingPort,
    candidate: Candidate,
    *,
    actor: str,
) -> tuple[int, int]:
    """One document, one transaction. Returns `(before, written)`."""
    assert candidate.report_ref is not None
    kbdoc = load_kbdoc(storage, candidate.report_ref)
    publisher = PublishService(session, embedder=embedder, audit=SqlAuditSink(session))
    result = publisher.rechunk(candidate.document_id, kbdoc, actor=actor)
    session.commit()
    return result.chunks_before, result.chunks_written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", help="one document id; default is every candidate")
    parser.add_argument(
        "--missing-subject-key",
        action="store_true",
        help="only documents whose live chunks carry no subject key at all",
    )
    parser.add_argument("--dry-run", action="store_true", help="report the work and change nothing")
    parser.add_argument("--actor", default="rechunk-corpus", help="recorded on the audit record")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("rechunk-corpus", settings.log_level, settings.log_format)
    engine = create_db_engine(settings.db)
    embedder = build_embedder()
    storage = S3StorageAdapter(settings.storage)

    print(f"embedder: {embedder.info.name} ({embedder.info.version})")
    with Session(engine) as session:
        found = candidates(session)
        if args.document:
            wanted = uuid.UUID(args.document)
            found = [c for c in found if c.document_id == wanted]
        if args.missing_subject_key:
            found = [c for c in found if c.needs_subject_key]

        skipped = [c for c in found if not c.rechunkable]
        work = [c for c in found if c.rechunkable]

        for candidate in skipped:
            # Not a failure: a seeded fixture has no derived JSON because nothing derived it.
            print(f"  skip  {candidate.title[:48]:50} no stored KBDoc")
        for candidate in work:
            print(
                f"  {'plan' if args.dry_run else 'run '}  {candidate.title[:48]:50} "
                f"{candidate.chunks:4} chunks, {candidate.keyed:4} keyed"
            )

        if args.dry_run:
            print(f"\ndry run: {len(work)} document(s) would be rechunked, nothing changed")
            return 0

        total_before = total_after = 0
        for candidate in work:
            before, written = rechunk_one(session, storage, embedder, candidate, actor=args.actor)
            total_before += before
            total_after += written
            print(f"  done  {candidate.title[:48]:50} {before} → {written} chunks")

    print(f"\nrechunked {len(work)} document(s): {total_before} → {total_after} chunks")
    if skipped:
        print(f"skipped {len(skipped)} without a stored KBDoc")
    print("chunk ids changed; audit retrieve records from before this run no longer resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
