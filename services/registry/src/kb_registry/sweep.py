"""The daily expiry sweep — bookkeeping only (ADR-0031).

Expiry is the one fact in this domain that becomes true while everybody is asleep. Every other
state change has somebody doing something and a transaction to hang correctness on; a document
ceasing to apply on 1 May has no such event.

**What this does not do is decide.** Every judgement — is this instrument really finished, on
what date, on whose authority — happened when the ledger row was written, in front of a person.
This reads confirmed rows and a clock. That is what makes it safe to run unattended against a
bank's corpus, and it is why the job is idempotent: running it twice, or running it after a
three-day outage, produces the same state.

**And serving does not depend on it running.** The dates were projected onto the chunk copies
at confirmation, so a chunk whose end date was written in February stops being retrievable on
1 May with this job switched off. The failure mode here is *late banners and queues*, never
*wrong answers* — which is the property the tests assert, by never running the sweep at all.

Three pieces of bookkeeping only a calendar can trigger:

* flip `documents.status` to `expired` where a confirmed whole-document date has passed;
* warn at T-30 for confirmed expiries approaching, and when `review_by` comes round;
* emit the ordinary publish event so a keyword backend living outside Postgres follows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from kb_common.audit import AuditSink
from kb_common.logging import get_logger
from kb_schemas.enums import DocStatus, ReviewTaskState, ReviewTaskType
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_registry import repository as repo
from kb_registry.publish import TOPIC_PUBLISHED
from kb_registry.service import RegistryService

log = get_logger(__name__)

#: How much warning a steward gets before an instrument leaves service. Long enough to object
#: through the ordinary channels, short enough that the task is still about *this* expiry.
WARNING_WINDOW = timedelta(days=30)

#: Only these rows are acted on: confirmed, current belief, and about the whole document. A row
#: carrying anchors is a partial expiry — it takes clauses out of service, not the instrument,
#: so flipping the document's status on one would be wrong (ADR-0040).
_DUE_EXPIRIES = """
    SELECT e.document_id, e.effective_to, d.canonical_version_id, d.category_path, d.title
    FROM document_expiry e
    JOIN documents d ON d.id = e.document_id
    WHERE e.state = 'confirmed'
      AND e.closed_at IS NULL
      AND e.anchors IS NULL
      AND e.effective_to < :today
      AND d.status = :published
"""

_APPROACHING_EXPIRIES = """
    SELECT e.document_id, e.effective_to, e.evidence, d.canonical_version_id, d.category_path
    FROM document_expiry e
    JOIN documents d ON d.id = e.document_id
    WHERE e.state = 'confirmed'
      AND e.closed_at IS NULL
      AND e.anchors IS NULL
      AND e.effective_to >= :today
      AND e.effective_to <= :horizon
      AND d.status = :published
      AND d.canonical_version_id IS NOT NULL
"""

_DUE_ATTESTATIONS = """
    SELECT d.id, d.review_by, d.canonical_version_id, d.category_path
    FROM documents d
    WHERE d.review_by IS NOT NULL
      AND d.review_by <= :today
      AND d.status = :published
      AND d.canonical_version_id IS NOT NULL
"""


@dataclass
class SweepResult:
    ran_for: date
    expired: list[uuid.UUID] = field(default_factory=list)
    warned: list[uuid.UUID] = field(default_factory=list)
    attestations: list[uuid.UUID] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ran_for": self.ran_for.isoformat(),
            "expired": [str(item) for item in self.expired],
            "warned": [str(item) for item in self.warned],
            "attestations": [str(item) for item in self.attestations],
        }


class ExpirySweep:
    def __init__(self, session: Session, *, audit: AuditSink | None = None) -> None:
        self._session = session
        self._registry = RegistryService(session, audit=audit)

    def run(self, *, today: date | None = None) -> SweepResult:
        when = today or date.today()
        return SweepResult(
            ran_for=when,
            expired=self.flip_expired(today=when),
            warned=self.warn_approaching(today=when),
            attestations=self.open_attestations(today=when),
        )

    def flip_expired(self, *, today: date | None = None) -> list[uuid.UUID]:
        """Relabel documents whose confirmed expiry date has passed.

        Cosmetic by design. Nothing reads `documents.status` for effectivity — the chunk dates
        already did the work — so this is what makes the archive-reader role's `expired`
        privilege reach a populated set, and what a steward sees on a list screen. The
        invariant to keep is that nothing ever *starts* reading status for effectivity.
        """
        when = today or date.today()
        rows = self._session.execute(
            text(_DUE_EXPIRIES), {"today": when, "published": DocStatus.PUBLISHED.value}
        ).all()

        flipped: list[uuid.UUID] = []
        for row in rows:
            self._session.execute(
                text("UPDATE documents SET status = :expired, updated_at = now() WHERE id = :id"),
                {"expired": DocStatus.EXPIRED.value, "id": row.document_id},
            )
            # The same event a publish emits. A keyword backend outside Postgres has no other
            # way to learn that this document's status moved.
            repo.enqueue(
                self._session,
                TOPIC_PUBLISHED,
                {
                    "document_id": str(row.document_id),
                    "version_id": str(row.canonical_version_id)
                    if row.canonical_version_id
                    else None,
                    "status": DocStatus.EXPIRED.value,
                    "expired_on": row.effective_to.isoformat(),
                },
            )
            flipped.append(row.document_id)
            log.info(
                "document_marked_expired",
                extra={"document_id": str(row.document_id), "expired_on": str(row.effective_to)},
            )
        return flipped

    def warn_approaching(self, *, today: date | None = None) -> list[uuid.UUID]:
        """Open an `expiry_review` task 30 days out, once per expiry."""
        when = today or date.today()
        rows = self._session.execute(
            text(_APPROACHING_EXPIRIES),
            {
                "today": when,
                "horizon": when + WARNING_WINDOW,
                "published": DocStatus.PUBLISHED.value,
            },
        ).all()

        warned: list[uuid.UUID] = []
        for row in rows:
            if self._already_open(row.canonical_version_id, ReviewTaskType.EXPIRY_REVIEW):
                continue
            self._registry.open_review_task(
                row.canonical_version_id,
                ReviewTaskType.EXPIRY_REVIEW,
                assignee_group=self._registry.steward_group_for(str(row.category_path)),
                payload={
                    "document_id": str(row.document_id),
                    "expires_on": row.effective_to.isoformat(),
                    "days_remaining": (row.effective_to - when).days,
                    # The sentence the date was read from, so the steward can object without
                    # opening the instrument (ADR-0029's discipline, one screen further on).
                    "evidence": row.evidence,
                },
            )
            warned.append(row.document_id)
        return warned

    def open_attestations(self, *, today: date | None = None) -> list[uuid.UUID]:
        """Open a `periodic_review` task where `review_by` has come round.

        The column has been settable and indexed since the initial schema and read by nothing.
        Periodic re-attestation of internal procedures is the reason it exists.
        """
        when = today or date.today()
        rows = self._session.execute(
            text(_DUE_ATTESTATIONS), {"today": when, "published": DocStatus.PUBLISHED.value}
        ).all()

        opened: list[uuid.UUID] = []
        for row in rows:
            if self._already_open(row.canonical_version_id, ReviewTaskType.PERIODIC_REVIEW):
                continue
            self._registry.open_review_task(
                row.canonical_version_id,
                ReviewTaskType.PERIODIC_REVIEW,
                assignee_group=self._registry.steward_group_for(str(row.category_path)),
                payload={
                    "document_id": str(row.id),
                    "review_by": row.review_by.isoformat(),
                    "overdue_days": (when - row.review_by).days,
                },
            )
            opened.append(row.id)
        return opened

    def _already_open(self, version_id: uuid.UUID, task_type: ReviewTaskType) -> bool:
        """Idempotence. A daily job that re-opens yesterday's task is a job nobody can work."""
        found = self._session.execute(
            text(
                "SELECT 1 FROM review_tasks WHERE version_id = :version AND task_type = :type "
                "AND state IN (:open, :claimed) LIMIT 1"
            ),
            {
                "version": version_id,
                "type": task_type.value,
                "open": ReviewTaskState.OPEN.value,
                "claimed": ReviewTaskState.CLAIMED.value,
            },
        ).scalar()
        return found is not None


__all__ = ["WARNING_WINDOW", "ExpirySweep", "SweepResult"]
