"""The merge screen and its approvals.

Three panes, one screen: what is canonical today, what the amendment says, and what the
consolidated text would be. The reviewer's job is to decide whether the third one is right,
and the payload here is shaped for exactly that — one row per changed section, carrying its
old text, its new text, the word-level diff between them, the model's bucket and the drafted
consolidation. A screen that made the reviewer align two documents by eye would get skimmed,
and a skimmed consolidation is how a bank publishes law it has not read.

Approvals are recorded here and *signalled* to the workflow; the publish itself stays in
`MergeFlow` (INV-5: one publish path). Recording them here as well is not duplication — it is
what lets the portal refuse a bad approval synchronously, with a reason the approver can read,
instead of accepting a click that the workflow will silently drop.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_identity_merge.diff import DocumentDiff, SectionChange, diff_documents, word_diff
from kb_ports.storage import StoragePort
from kb_registry import repository as repo
from kb_schemas.enums import ReviewTaskState, ReviewTaskType
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import ReviewTaskRow
from kb_workflows.merge_policy import Approval, ApprovalError, ApprovalLedger, required_approvals
from sqlalchemy.orm import Session

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MergeDecision:
    decision: str  # approve | reject
    approver: str
    note: str = ""
    #: The draft the approver was looking at. An approval for a draft that has since been
    #: rebuilt is refused rather than counted.
    draft_ref: str = ""


@dataclass(frozen=True, slots=True)
class MergeDecisionOutcome:
    task_id: uuid.UUID
    decision: str
    required: int
    received: int
    satisfied: bool
    detail: str
    #: Whether the workflow waiting on these approvals was told. Set by the route: the service
    #: records the decision, the route delivers it.
    signalled: bool = False


class MergeService:
    def __init__(
        self,
        session: Session,
        *,
        storage: StoragePort,
        audit: AuditSink | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._audit = audit

    # -------------------------------------------------------------------------- reading

    def screen(self, task_id: uuid.UUID, *, reviewer_groups: set[str]) -> dict[str, Any]:
        """Everything the three panes render, in one round trip."""
        row = self._task(task_id, reviewer_groups)
        payload = dict(row.payload or {})

        new_version = repo.get_version(self._session, row.version_id)
        if new_version is None:  # pragma: no cover - referential integrity guarantees this
            raise NotFound("version not found", version_id=str(row.version_id))
        document = repo.get_document(self._session, new_version.document_id)
        if document is None:  # pragma: no cover
            raise NotFound("document not found")
        canonical = repo.get_canonical_version(self._session, document.id)

        old_doc = self._load(canonical.idp_report_ref if canonical else None)
        new_doc = self._load(new_version.idp_report_ref)
        if new_doc is None:
            raise Conflict("the incoming version has no parsed text to merge")
        # Recomputed rather than read back from the stored draft: the diff is deterministic and
        # cheap, and the stored form drops the word spans the middle pane is made of.
        diff = diff_documents(old_doc or _empty_document(), new_doc)
        draft = self._load_draft(payload.get("draft_ref"))

        return {
            "task": {
                "id": row.id,
                "task_type": row.task_type,
                "state": row.state,
                "assignee_group": row.assignee_group,
                "consolidation": bool(payload.get("consolidation")),
                "payload": payload,
            },
            "document": {
                "id": document.id,
                "title": document.title,
                "legal_number": document.legal_number,
                "doc_class": document.doc_class,
                "category_path": document.category_path,
            },
            "amending_document": self._amending(payload.get("amending_document_id")),
            "current_canonical": self._pane(canonical, old_doc),
            "new_version": self._pane(new_version, new_doc),
            "llm_draft": {
                "draft_ref": payload.get("draft_ref"),
                "complete": bool(draft.get("complete", False)) if draft else False,
                "model": draft.get("model", "") if draft else "",
                "prompt_version": draft.get("prompt_version", "") if draft else "",
                "substantive_changes": draft.get("substantive_changes", 0) if draft else 0,
                # An absent draft is a fact the reviewer needs, not an empty pane to puzzle at.
                "available": draft is not None,
            },
            "section_classifications": self._sections(diff, draft),
            "summary": diff.summary(),
            "touched_articles": diff.touched_articles,
            "approvals": self._ledger_state(row, payload),
        }

    # ------------------------------------------------------------------------- deciding

    def decide(
        self, task_id: uuid.UUID, decision: MergeDecision, *, reviewer_groups: set[str]
    ) -> MergeDecisionOutcome:
        """Record one approval or rejection. Publishing is the workflow's job, not this one's."""
        row = self._task(task_id, reviewer_groups)
        if row.state == ReviewTaskState.DECIDED.value:
            raise Conflict("this merge has already been decided", task_id=str(task_id))
        if decision.decision not in ("approve", "reject"):
            raise ValidationError("decision must be approve or reject")

        payload = dict(row.payload or {})
        ledger = self._ledger(payload)

        if decision.decision == "reject":
            ledger.reject(decision.approver, decision.note)
            self._persist(row, payload, ledger)
            self._close(row, decision, outcome="rejected")
            self._record(decision, row, outcome="rejected")
            return MergeDecisionOutcome(
                task_id=task_id,
                decision="reject",
                required=ledger.required,
                received=len(ledger.approvals),
                satisfied=False,
                detail=decision.note or "rejected by the reviewer",
            )

        try:
            ledger.add(
                Approval(
                    approver=decision.approver,
                    note=decision.note,
                    draft_ref=decision.draft_ref,
                )
            )
        except ApprovalError as exc:
            # Four-eyes refusals are the approver's problem to fix, not an internal error.
            raise Conflict(str(exc), task_id=str(task_id)) from exc

        self._persist(row, payload, ledger)
        if ledger.satisfied:
            # The task stays open until the workflow has published; the approver is not the
            # one who decides that it worked.
            self._close(row, decision, outcome="approved")
        self._record(decision, row, outcome="approved")

        log.info(
            "merge_approval_recorded",
            extra={
                "task_id": str(task_id),
                "approver": decision.approver,
                "received": len(ledger.approvals),
                "required": ledger.required,
                "satisfied": ledger.satisfied,
            },
        )
        return MergeDecisionOutcome(
            task_id=task_id,
            decision="approve",
            required=ledger.required,
            received=len(ledger.approvals),
            satisfied=ledger.satisfied,
            detail=(
                "approved; the consolidation will publish"
                if ledger.satisfied
                else f"approved; {ledger.required - len(ledger.approvals)} more approval(s) needed"
            ),
        )

    # ------------------------------------------------------------------------ internals

    def _task(self, task_id: uuid.UUID, reviewer_groups: set[str]) -> ReviewTaskRow:
        row = repo.get_review_task(self._session, task_id)
        if row is None or row.task_type != ReviewTaskType.MERGE_REVIEW.value:
            raise NotFound("merge task not found", task_id=str(task_id))
        if row.assignee_group and row.assignee_group not in reviewer_groups:
            # Not "forbidden": whether the Legal cell is consolidating something is itself
            # information a reviewer outside it has no business learning.
            raise NotFound("merge task not found", task_id=str(task_id))
        return row

    def _ledger(self, payload: dict[str, Any]) -> ApprovalLedger:
        ledger = ApprovalLedger(
            doc_class=str(payload.get("doc_class") or ""),
            author=str(payload.get("prepared_by") or ""),
            draft_ref=str(payload.get("draft_ref") or ""),
        )
        for item in payload.get("approvals") or []:
            # Replayed without the guards: these were already checked when they arrived, and
            # re-checking them here would reject a legitimate second approval.
            ledger.approvals.append(
                Approval(
                    approver=str(item["approver"]),
                    note=str(item.get("note") or ""),
                    draft_ref=str(item.get("draft_ref") or ""),
                )
            )
        if payload.get("rejected_by"):
            ledger.rejected_by = str(payload["rejected_by"])
            ledger.rejection_reason = str(payload.get("rejection_reason") or "")
        return ledger

    def _persist(self, row: ReviewTaskRow, payload: dict[str, Any], ledger: ApprovalLedger) -> None:
        payload["approvals"] = [
            {
                "approver": approval.approver,
                "note": approval.note,
                "draft_ref": approval.draft_ref,
                "at": datetime.now(UTC).isoformat(),
            }
            for approval in ledger.approvals
        ]
        payload["rejected_by"] = ledger.rejected_by
        payload["rejection_reason"] = ledger.rejection_reason
        # Reassigned rather than mutated: SQLAlchemy does not track in-place JSONB edits.
        row.payload = payload
        self._session.flush()

    def _ledger_state(self, row: ReviewTaskRow, payload: dict[str, Any]) -> dict[str, Any]:
        ledger = self._ledger(payload)
        state = ledger.status()
        state["doc_class"] = payload.get("doc_class")
        state["prepared_by"] = payload.get("prepared_by")
        state["required"] = required_approvals(str(payload.get("doc_class") or ""))
        state["decided"] = row.state == ReviewTaskState.DECIDED.value
        return state

    def _close(self, row: ReviewTaskRow, decision: MergeDecision, *, outcome: str) -> None:
        row.state = ReviewTaskState.DECIDED.value
        row.decision = outcome
        row.decided_by = decision.approver
        row.decided_at = datetime.now(UTC)
        self._session.flush()

    def _record(self, decision: MergeDecision, row: ReviewTaskRow, *, outcome: str) -> None:
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(
                action=AuditAction.MERGE_APPROVAL,
                actor=decision.approver,
                object_ref={"task_id": str(row.id), "version_id": str(row.version_id)},
                detail={
                    "decision": outcome,
                    "note": decision.note,
                    "draft_ref": decision.draft_ref,
                },
            )
        )

    def _pane(self, version: Any, doc: KBDoc | None) -> dict[str, Any] | None:
        if version is None:
            return None
        return {
            "version_id": version.id,
            "author": version.author,
            "created_at": version.created_at,
            "pii_status": version.pii_status,
            "is_canonical": version.is_canonical,
            "blocks": [
                {
                    "id": block.id,
                    "text": block.text,
                    "type": block.type,
                    "section_path": list(block.section_path),
                }
                for block in (doc.blocks if doc else [])
            ],
        }

    def _sections(self, diff: DocumentDiff, draft: dict[str, Any] | None) -> list[dict[str, Any]]:
        """One row per changed section: the three panes, aligned."""
        payload = draft or {}
        buckets = {
            str(item["section_path"]): item for item in (payload.get("classifications") or [])
        }
        drafted = {str(item["section_path"]): item for item in (payload.get("sections") or [])}

        rows: list[dict[str, Any]] = []
        for change in diff.changed:
            classification = buckets.get(change.section_path, {})
            section = drafted.get(change.section_path, {})
            rows.append(
                {
                    "section_path": change.section_path,
                    "article": change.article,
                    "kind": change.kind.value,
                    "similarity": round(change.similarity, 3),
                    "old_text": change.old_text,
                    "new_text": change.new_text,
                    "spans": [{"op": span.op, "text": span.text} for span in self._spans(change)],
                    "bucket": classification.get("bucket"),
                    "impact": classification.get("impact", ""),
                    # True when the model did not speak and the diff's own verdict was used.
                    # The screen marks these; a reviewer must know what was guessed.
                    "inferred": bool(classification.get("inferred", True)),
                    "consolidated_text": section.get("consolidated_text", change.new_text),
                    "drafted": bool(section.get("drafted", False)),
                    "note": section.get("note", ""),
                }
            )
        return rows

    def _spans(self, change: SectionChange) -> tuple[Any, ...]:
        if change.spans:
            return change.spans
        return word_diff(change.old_text, change.new_text)

    def _amending(self, document_id: Any) -> dict[str, Any] | None:
        if not document_id:
            return None
        document = repo.get_document(self._session, uuid.UUID(str(document_id)))
        if document is None:
            return None
        return {
            "id": document.id,
            "title": document.title,
            "legal_number": document.legal_number,
        }

    def workflow_id(self, task_id: uuid.UUID, *, reviewer_groups: set[str]) -> str:
        """The workflow waiting on this task's approvals, if the task knows about one."""
        row = self._task(task_id, reviewer_groups)
        return str((row.payload or {}).get("workflow_id") or "")

    def _load(self, ref: str | None) -> KBDoc | None:
        if not ref:
            return None
        bucket, _, key = str(ref).partition("/")
        try:
            return KBDoc.model_validate_json(self._storage.get(bucket, key))
        except NotFound:
            log.warning("merge_kbdoc_missing", extra={"ref": ref})
            return None

    def _load_draft(self, ref: Any) -> dict[str, Any] | None:
        """The stored `{diff, draft}` artefact, or None when drafting never produced one."""
        if not ref:
            return None
        bucket, _, key = str(ref).partition("/")
        try:
            payload = json.loads(self._storage.get(bucket, key))
        except NotFound:
            log.warning("merge_draft_missing", extra={"ref": ref})
            return None
        draft = payload.get("draft")
        return dict(draft) if isinstance(draft, dict) else None


def _empty_document() -> KBDoc:
    """A first consolidation has nothing canonical to diff against; every section is new."""
    from kb_schemas.kbdoc import DocMeta

    return KBDoc(doc_meta=DocMeta(source_format="txt"))
