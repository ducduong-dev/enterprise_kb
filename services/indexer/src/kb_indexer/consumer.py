"""Outbox consumer — Postgres to the keyword index.

Postgres is already consistent when the publish transaction commits (INV-5). This process
propagates that commit to the external keyword index, which cannot join the transaction. The
outbox is what makes the two converge: an event exists if and only if the state change
committed, so a crash anywhere in this loop is a delay, never a divergence.

Properties the recovery story depends on:

* **At-least-once, idempotent.** Events are claimed with `FOR UPDATE SKIP LOCKED`, and the
  work — indexing a version's chunks by chunk id, deleting the previous version's — produces
  the same result however many times it runs. Killing this process mid-publish and restarting
  leaves the index correct.
* **Ordered per document.** Claiming by id keeps a document's publish events in order, so a
  fast second publish cannot be overtaken by its predecessor's write.
* **Failures retry, they do not vanish.** A failed event stays unprocessed with its attempt
  count and last error recorded; the backlog gauge and its alert make that visible.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kb_common.logging import get_logger
from kb_common.metrics import outbox_processed, outbox_unprocessed, publish_to_searchable
from kb_ports.indexes import IndexDocument, KeywordIndexPort
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

TOPIC_PUBLISHED = "registry.published"

#: Events claimed per poll. Small enough that a failure retries quickly, large enough that a
#: backfill is not one round trip per document.
BATCH_SIZE = 20
#: After this many attempts an event is left alone and alerted on rather than retried forever.
MAX_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    processed: int
    failed: int
    skipped: int = 0


class OutboxConsumer:
    def __init__(self, session: Session, *, keyword_index: KeywordIndexPort) -> None:
        self._session = session
        self._keyword = keyword_index

    # ------------------------------------------------------------------------ polling

    def run_once(self, *, batch_size: int = BATCH_SIZE) -> ConsumeResult:
        """Claim and handle one batch. Returns counts; never raises for a single bad event."""
        events = self._claim(batch_size)
        processed = failed = skipped = 0

        for event in events:
            topic = event["topic"]
            try:
                if topic == TOPIC_PUBLISHED:
                    self._handle_published(event["payload"])
                else:
                    # Unknown topics are marked done rather than blocking the queue: a future
                    # service's events must not stall the index.
                    skipped += 1
                    log.info("outbox_topic_ignored", extra={"topic": topic})
                self._mark_processed(event["id"])
                processed += 1
                outbox_processed.labels(topic=topic, outcome="ok").inc()
            except Exception as exc:
                failed += 1
                self._mark_failed(event["id"], exc)
                outbox_processed.labels(topic=topic, outcome="error").inc()
                log.error(
                    "outbox_event_failed",
                    extra={"outbox_id": event["id"], "topic": topic, "error": str(exc)},
                )

        self._session.commit()
        self._refresh_backlog_gauge()
        return ConsumeResult(processed=processed, failed=failed, skipped=skipped)

    def run_forever(self, *, poll_seconds: float = 1.0) -> None:  # pragma: no cover - loop
        log.info("outbox_consumer_started", extra={"poll_seconds": poll_seconds})
        while True:
            result = self.run_once()
            if result.processed == 0:
                time.sleep(poll_seconds)

    # ------------------------------------------------------------------------ handlers

    def _handle_published(self, payload: dict[str, Any]) -> None:
        version_id = uuid.UUID(payload["version_id"])
        document_id = uuid.UUID(payload["document_id"])
        previous = payload.get("previous_version_id")

        documents = self._load_chunks(version_id)
        if not documents:
            # The version was superseded before we got here. Its chunks are already
            # tombstoned; indexing them would resurrect content the registry retired.
            log.info("publish_event_stale", extra={"version_id": str(version_id)})
        else:
            self._keyword.upsert(documents)

        if previous:
            # Delete before/after does not matter for correctness — the ACL filter excludes
            # tombstoned chunks either way — but doing it after the upsert means there is no
            # instant where neither version is searchable.
            self._keyword.tombstone([uuid.UUID(previous)])

        self._observe_latency(payload)
        log.info(
            "version_indexed",
            extra={
                "document_id": str(document_id),
                "version_id": str(version_id),
                "chunks": len(documents),
                "superseded": previous,
            },
        )

    def _observe_latency(self, payload: dict[str, Any]) -> None:
        """Publish-to-searchable, the number INV-5's 10 s budget is about."""
        published_at = payload.get("published_at")
        if not published_at:
            return
        try:
            committed = datetime.fromisoformat(str(published_at))
        except ValueError:  # pragma: no cover - malformed payload
            return
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        publish_to_searchable.observe((datetime.now(UTC) - committed).total_seconds())

    # ------------------------------------------------------------------------ plumbing

    def _claim(self, batch_size: int) -> list[dict[str, Any]]:
        rows = (
            self._session.execute(
                text(
                    """
                SELECT id, topic, payload
                FROM outbox
                WHERE processed_at IS NULL AND attempts < :max_attempts
                ORDER BY id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
                ),
                {"limit": batch_size, "max_attempts": MAX_ATTEMPTS},
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def _load_chunks(self, version_id: uuid.UUID) -> list[IndexDocument]:
        rows = (
            self._session.execute(
                text(
                    """
                SELECT c.id, c.document_id, c.version_id, c.text, c.citation_label,
                       c.section_path, c.visibility, c.allowed_groups, c.department,
                       c.category_path::text AS category_path, c.doc_class, c.doc_status,
                       c.effective_from, c.effective_to, c.tombstoned, c.ordinal, c.page,
                       d.legal_number
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.version_id = :version_id AND NOT c.tombstoned
                ORDER BY c.ordinal
                """
                ),
                {"version_id": version_id},
            )
            .mappings()
            .all()
        )

        return [
            IndexDocument(
                chunk_id=row["id"],
                document_id=row["document_id"],
                version_id=row["version_id"],
                text=row["text"],
                citation_label=row["citation_label"],
                section_path=row["section_path"],
                visibility=row["visibility"],
                allowed_groups=list(row["allowed_groups"] or []),
                department=row["department"],
                doc_status=row["doc_status"],
                doc_class=row["doc_class"],
                category_path=row["category_path"],
                effective_from=row["effective_from"].isoformat() if row["effective_from"] else None,
                effective_to=row["effective_to"].isoformat() if row["effective_to"] else None,
                tombstoned=row["tombstoned"],
                extra={
                    "ordinal": row["ordinal"],
                    "page": row["page"],
                    "legal_number": row["legal_number"],
                },
            )
            for row in rows
        ]

    def _mark_processed(self, outbox_id: int) -> None:
        self._session.execute(
            text("UPDATE outbox SET processed_at = now() WHERE id = :id"), {"id": outbox_id}
        )

    def _mark_failed(self, outbox_id: int, error: Exception) -> None:
        self._session.execute(
            text("UPDATE outbox SET attempts = attempts + 1, last_error = :error WHERE id = :id"),
            {"id": outbox_id, "error": f"{type(error).__name__}: {error}"[:1000]},
        )

    def _refresh_backlog_gauge(self) -> None:
        pending = self._session.execute(
            text("SELECT count(*) FROM outbox WHERE processed_at IS NULL")
        ).scalar()
        outbox_unprocessed.set(float(pending or 0))
