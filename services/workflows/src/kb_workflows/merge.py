"""`MergeFlow` and `ConsolidationWorkflow`.

    diff → LLM draft → merge_review task → approval signal(s) → publish → impact tasks

The two differ in one respect the plan is explicit about: consolidation of a regulatory
instrument is the Legal cell's work and never auto-publishes (INV-8). Both otherwise share the
same activities, because the difference is *who decides*, not what happens.

Approvals arrive as Temporal signals. A consolidation typically waits days: the workflow holds
the draft, the review task and the approval ledger for as long as that takes, and a worker
restart mid-wait costs nothing. That is the whole reason this is a workflow rather than a
request handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from kb_workflows.merge_activities import (
        MergeDraftOutcome,
        MergeRequest,
        assess_impact,
        build_merge_draft,
        open_merge_review,
        publish_merged,
    )
    from kb_workflows.merge_policy import Approval, ApprovalError, ApprovalLedger

#: Drafting calls the model once per changed section; a large consolidation is slow but bounded.
_DRAFT_TIMEOUT = timedelta(minutes=30)
_DB_TIMEOUT = timedelta(seconds=60)
#: How long a merge waits for its approvals before giving up and leaving the task open. Six
#: weeks is deliberately generous: an unapproved consolidation is a document that stays
#: correct, not one that breaks.
_APPROVAL_TIMEOUT = timedelta(days=42)

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=["ValidationError", "GateBlocked", "Conflict"],
)


@dataclass
class MergeOutcome:
    status: str  # published | rejected | no_changes | approval_timeout
    document_id: str
    version_id: str
    review_task_id: str | None = None
    approvals: list[str] = field(default_factory=list)
    chunks_written: int = 0
    impacted_documents: int = 0
    impact_task_ids: list[str] = field(default_factory=list)
    detail: str = ""


@workflow.defn(name="MergeFlow")
class MergeFlow:
    """An incoming revision of a document already in the registry."""

    def __init__(self) -> None:
        self._status = "started"
        self._ledger: ApprovalLedger | None = None
        self._rejected = False
        self._errors: list[str] = []

    # ------------------------------------------------------------------------- signals

    @workflow.signal
    def approve(self, approver: str, note: str = "", draft_ref: str = "") -> None:
        """One approval. Refusals are recorded, not raised: a signal handler that throws
        would leave the approver with no feedback at all."""
        if self._ledger is None:
            self._errors.append("approval arrived before the draft was ready")
            return
        try:
            self._ledger.add(Approval(approver=approver, note=note, draft_ref=draft_ref))
        except ApprovalError as exc:
            self._errors.append(str(exc))

    @workflow.signal
    def reject(self, approver: str, reason: str = "") -> None:
        if self._ledger is not None:
            self._ledger.reject(approver, reason)
        self._rejected = True

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.query
    def approvals(self) -> dict[str, object]:
        """Polled by the merge screen: how many approvals, from whom, and what was refused."""
        base: dict[str, object] = self._ledger.status() if self._ledger else {"received": 0}
        base["errors"] = list(self._errors)
        return base

    # ----------------------------------------------------------------------------- run

    @workflow.run
    async def run(self, request: MergeRequest) -> MergeOutcome:
        self._status = "drafting"
        draft: MergeDraftOutcome = await workflow.execute_activity(
            build_merge_draft,
            request,
            start_to_close_timeout=_DRAFT_TIMEOUT,
            retry_policy=_RETRY,
        )

        if draft.no_changes:
            # The incoming text is identical to what is already canonical. Publishing it would
            # create a version whose only content is a new timestamp.
            self._status = "no_changes"
            return MergeOutcome(
                status="no_changes",
                document_id=request.document_id,
                version_id=request.new_version_id,
                detail="the incoming text is identical to the canonical version",
            )

        self._ledger = ApprovalLedger(
            doc_class=draft.doc_class,
            author=draft.author,
            draft_ref=draft.draft_ref or "",
        )

        self._status = "awaiting_approval"
        task_id = await workflow.execute_activity(
            open_merge_review,
            args=[request, draft],
            start_to_close_timeout=_DB_TIMEOUT,
            retry_policy=_RETRY,
        )

        try:
            await workflow.wait_condition(
                lambda: self._rejected or (self._ledger is not None and self._ledger.satisfied),
                timeout=_APPROVAL_TIMEOUT,
            )
            timed_out = False
        except TimeoutError:  # pragma: no cover - six-week timeout
            # Not an error: an unapproved consolidation is a document that stays correct. The
            # review task is left open and someone is still expected to decide.
            timed_out = True
        ledger = self._ledger
        assert ledger is not None

        if self._rejected:
            self._status = "rejected"
            return MergeOutcome(
                status="rejected",
                document_id=request.document_id,
                version_id=request.new_version_id,
                review_task_id=task_id,
                detail=ledger.rejection_reason or "rejected by the reviewer",
            )
        if timed_out and not ledger.satisfied:  # pragma: no cover - six-week timeout
            self._status = "approval_timeout"
            return MergeOutcome(
                status="approval_timeout",
                document_id=request.document_id,
                version_id=request.new_version_id,
                review_task_id=task_id,
                detail="the merge was not approved within the review window",
            )

        self._status = "publishing"
        # The last approver is recorded as the publishing actor: they are the person whose
        # decision made it canonical, and the audit record has to name someone.
        approver = ledger.approvals[-1].approver
        chunks = await workflow.execute_activity(
            publish_merged,
            args=[request, approver, ledger.approvals[-1].note],
            start_to_close_timeout=_DB_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._status = "assessing_impact"
        impact = await workflow.execute_activity(
            assess_impact,
            args=[request, draft.touched_articles],
            start_to_close_timeout=_DB_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._status = "published"
        return MergeOutcome(
            status="published",
            document_id=request.document_id,
            version_id=request.new_version_id,
            review_task_id=task_id,
            approvals=sorted(ledger.approvers),
            chunks_written=chunks,
            impacted_documents=impact.impacted,
            impact_task_ids=impact.task_ids,
            detail=f"published after {len(ledger.approvers)} approval(s)",
        )


@workflow.defn(name="ConsolidationWorkflow")
class ConsolidationWorkflow(MergeFlow):
    """Applying an amending instrument to produce `văn bản hợp nhất`.

    Same machinery as `MergeFlow`; the differences are that the review lands with the Legal
    cell and that the document class involved always requires two approvers (INV-8). Both are
    enforced by the activity and the ledger rather than here, so a caller cannot start this
    workflow in a way that skips either.
    """

    @workflow.run
    async def run(self, request: MergeRequest) -> MergeOutcome:
        consolidation_request = MergeRequest(
            document_id=request.document_id,
            new_version_id=request.new_version_id,
            actor=request.actor,
            amending_document_id=request.amending_document_id,
            consolidation=True,
        )
        return await super().run(consolidation_request)
