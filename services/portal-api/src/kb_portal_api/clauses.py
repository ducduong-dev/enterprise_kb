"""The clause-review screen: two clauses side by side, and one decision.

The steward-facing end of M9d. A funnel run proposes that one clause replaced another and opens
a `clause_review` task; nothing it proposed is visible to any reader until somebody working this
screen confirms it (ADR-0033).

What the screen has to make easy is a *comparison*, not an audit of the detector. So the two
texts are shown whole and adjacent, the quantity delta is rendered as the change it is —
"8%/năm → 10%/năm" — and the model's rationale sits beside them rather than above them. A
similarity score is deliberately absent from the layout's centre: it is on the record for the
eval, and it is the one number that would invite a reviewer to defer to the machine instead of
reading two paragraphs.

**The decision is the ledger's, not this screen's.** Confirming calls
`ClauseSupersessions.confirm`, which is where four-eyes lives (INV-8) and where the append-only
two-clock discipline is kept. Rejecting calls `revoke`, which writes a new row rather than
deleting one — a supersession somebody looked at and rejected is a finding about the detector,
and deleting it loses the only record that the pair was ever considered.

**Access is the task's `assignee_group`, exactly as on the merge screen.** A task nobody in the
reviewer's groups owns is reported as missing rather than forbidden, because whether the bank is
reviewing a supersession is itself something an outsider has no business learning. The clause
texts are then shown without a second ACL pass, which is the same trade the merge screen makes:
the group that stewards the document is the audience, and a reviewer who cannot see both halves
of a comparison cannot make the comparison.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import NotFound, ValidationError
from kb_common.logging import get_logger
from kb_registry import repository as repo
from kb_registry.supersession import ClauseSupersessions
from kb_schemas.enums import ReviewTaskState, ReviewTaskType
from kb_schemas.orm import ReviewTaskRow
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: How the funnel reached the verdict, in the reviewer's words. `direction` is the interesting
#: one: the model judged the pair a supersession and the dates would not confirm which way, so
#: it is here precisely because nothing else could settle it.
SETTLED_BY_LABEL = {
    "model": "Mô hình đối chiếu hai điều khoản",
    "direction": "Mô hình và ngày hiệu lực không thống nhất về chiều thay thế",
    "scope": "Đối tượng áp dụng khác nhau (không gọi mô hình)",
    "identical_text": "Nội dung trùng khớp (không gọi mô hình)",
}

_CLAUSE = """
    SELECT c.text, c.citation_label, c.section_path, c.effective_from, c.effective_to,
           d.title, d.legal_number, d.doc_class
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.document_id = :document_id
      AND c.section_path = :section_path
      AND NOT c.tombstoned
    LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class ClauseDecision:
    decision: str
    actor: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class ClauseDecisionOutcome:
    task_id: uuid.UUID
    decision: str
    state: str
    detail: str


class ClauseReviewService:
    def __init__(self, session: Session, *, audit: AuditSink | None = None) -> None:
        self._session = session
        self._audit = audit
        self._ledger = ClauseSupersessions(session, audit=audit)

    def screen(self, task_id: uuid.UUID, *, reviewer_groups: set[str]) -> dict[str, Any]:
        """Everything the comparison needs, and nothing that ranks it."""
        row = self._task(task_id, reviewer_groups)
        payload = dict(row.payload or {})
        record = self._row(payload)

        old = self._clause(record.old_document_id, record.old_section_path)
        new = (
            self._clause(record.new_document_id, record.new_section_path)
            if record.new_document_id and record.new_section_path
            else None
        )
        delta = payload.get("quantity_delta") or {}
        return {
            "task_id": str(row.id),
            "supersession_id": str(record.id),
            "state": record.state,
            "decided": row.state == ReviewTaskState.DECIDED.value,
            "assignee_group": row.assignee_group,
            # Both clauses whole. A truncated pane is a reviewer approving text they did not
            # read, which is the failure this whole gate exists to prevent.
            "old": old,
            "new": new,
            "supersedes_from": record.supersedes_from.isoformat(),
            "basis": record.basis,
            "verdict": record.verdict,
            "settled_by": payload.get("settled_by"),
            "settled_by_label": SETTLED_BY_LABEL.get(str(payload.get("settled_by")), ""),
            #: The change, as a change. `["8%/năm → 10%/năm"]`, already computed old→new.
            "quantity_delta": list(delta.get("changed") or []),
            "scope_facets": record.scope_facets or {},
            "rationale": record.evidence or payload.get("rationale") or "",
            "model": record.model,
            "prompt_version": record.prompt_version,
            "detected_by": record.detected_by,
            #: Present for the record rather than for the layout — see the module docstring.
            "score": record.score,
        }

    def decide(
        self, task_id: uuid.UUID, decision: ClauseDecision, *, reviewer_groups: set[str]
    ) -> ClauseDecisionOutcome:
        """Confirm the supersession or reject it. Either way the task closes.

        A rejection needs a note and the ledger enforces it: "why was this not a supersession"
        is the only signal the detector's false-positive rate can be measured from, and a
        reviewer who rejects in silence has taken that measurement away.
        """
        row = self._task(task_id, reviewer_groups)
        if row.state == ReviewTaskState.DECIDED.value:
            raise ValidationError("this task has already been decided", task_id=str(task_id))
        record = self._row(dict(row.payload or {}))

        if decision.decision == "confirm":
            outcome = self._ledger.confirm(record.id, actor=decision.actor)
        elif decision.decision == "reject":
            if not decision.note.strip():
                raise ValidationError("a rejection needs a reason")
            outcome = self._ledger.revoke(record.id, actor=decision.actor, reason=decision.note)
        else:
            raise ValidationError(
                "decision must be 'confirm' or 'reject'", decision=decision.decision
            )

        self._close(row, decision)
        self._record(row, decision, outcome.state)
        return ClauseDecisionOutcome(
            task_id=row.id, decision=decision.decision, state=outcome.state, detail=outcome.note
        )

    # ------------------------------------------------------------------------ internals

    def _task(self, task_id: uuid.UUID, reviewer_groups: set[str]) -> ReviewTaskRow:
        row = repo.get_review_task(self._session, task_id)
        if row is None or row.task_type != ReviewTaskType.CLAUSE_REVIEW.value:
            raise NotFound("clause review task not found", task_id=str(task_id))
        if row.assignee_group and row.assignee_group not in reviewer_groups:
            # Not "forbidden", for the reason the merge screen gives: whether the bank is
            # reviewing a supersession is itself something an outsider should not learn.
            raise NotFound("clause review task not found", task_id=str(task_id))
        return row

    def _row(self, payload: dict[str, Any]) -> Any:
        """The `clause_supersessions` row this task was opened for.

        Read fresh rather than from the payload. The payload is a snapshot taken when the
        funnel ran; the ledger is the authority, and a steward who opened the screen after
        somebody else confirmed the same row must see that rather than a stale proposal.
        """
        raw = payload.get("supersession_id")
        if not raw:
            raise NotFound("this task names no supersession")
        from kb_schemas.orm import ClauseSupersessionRow

        record = self._session.get(ClauseSupersessionRow, uuid.UUID(str(raw)))
        if record is None:
            raise NotFound("supersession not found", supersession_id=str(raw))
        return record

    def _clause(self, document_id: uuid.UUID, section_path: str) -> dict[str, Any]:
        """One pane. Missing text is reported, never blank.

        A rechunk can retire the exact `section_path` a proposal was anchored on, and a pane
        that silently rendered empty would read as "this clause says nothing" — which a
        reviewer could plausibly confirm.
        """
        found = (
            self._session.execute(
                text(_CLAUSE), {"document_id": document_id, "section_path": section_path}
            )
            .mappings()
            .one_or_none()
        )
        if found is None:
            return {
                "document_id": str(document_id),
                "section_path": section_path,
                "missing": True,
            }
        return {
            "document_id": str(document_id),
            "section_path": section_path,
            "citation_label": found["citation_label"],
            "text": found["text"],
            "document_title": found["title"],
            "legal_number": found["legal_number"],
            "doc_class": found["doc_class"],
            "effective_from": (
                found["effective_from"].isoformat() if found["effective_from"] else None
            ),
            "effective_to": found["effective_to"].isoformat() if found["effective_to"] else None,
            "missing": False,
        }

    def _close(self, row: ReviewTaskRow, decision: ClauseDecision) -> None:
        row.state = ReviewTaskState.DECIDED.value
        row.decision = decision.decision
        row.decided_by = decision.actor
        row.decided_at = datetime.now(UTC)
        self._session.flush()

    def _record(self, row: ReviewTaskRow, decision: ClauseDecision, state: str) -> None:
        """That the *task* was closed, and by whom.

        `REVIEW_DECISION`, the same action every other queue writes when a task leaves it — and
        deliberately not a second record of the supersession itself. `ClauseSupersessions`
        already wrote `CLAUSE_SUPERSESSION` for the confirm or the revoke, from inside the
        transaction that made it true, and two records of one decision is how an auditor ends
        up reconciling the platform against itself.
        """
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(
                action=AuditAction.REVIEW_DECISION,
                actor=decision.actor,
                object_ref={"task_id": str(row.id), "version_id": str(row.version_id)},
                detail={
                    "task_type": ReviewTaskType.CLAUSE_REVIEW.value,
                    "decision": decision.decision,
                    "state": state,
                    "note": decision.note,
                },
            )
        )


__all__ = ["SETTLED_BY_LABEL", "ClauseDecision", "ClauseDecisionOutcome", "ClauseReviewService"]
