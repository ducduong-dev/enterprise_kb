"""`IngestWorkflow` — the M1 happy path.

    stored original → IDP → register document + version → link references → human task

Everything durable lives in Temporal: a document waiting three weeks for a reviewer costs
nothing while it waits, and a service restart mid-ingest resumes rather than losing the
upload. Human steps arrive as signals (M3 onwards); M1 ends by opening the task.

The branch logic is in `routing.decide` so it can be tested without a Temporal server.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from kb_workflows.activities import (
        link_detected_refs,
        open_review_task,
        register_ingest,
        run_idp,
        scan_pii,
    )
    from kb_workflows.routing import decide
    from kb_workflows.types import IdpOutcome, IngestOutcome, IngestRequest, PiiOutcome

#: Parsing a 200-page scanned PDF is slow but bounded; a longer run means something is wrong.
_IDP_TIMEOUT = timedelta(minutes=15)
_DB_TIMEOUT = timedelta(seconds=30)
#: The PII gate reads every block and may consult a model; generous, but bounded, because a
#: gate that never returns is a document that never publishes.
_PII_TIMEOUT = timedelta(minutes=10)

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
    # A malformed document will fail identically on every retry; fail fast and let a human
    # see it rather than burning the queue on it.
    non_retryable_error_types=["ValidationError", "UnsupportedFormat"],
)


@workflow.defn(name="IngestWorkflow")
class IngestWorkflow:
    def __init__(self) -> None:
        self._status = "started"

    @workflow.query
    def status(self) -> str:
        """Polled by the portal's upload screen."""
        return self._status

    @workflow.run
    async def run(self, request: IngestRequest) -> IngestOutcome:
        self._status = "parsing"
        idp: IdpOutcome = await workflow.execute_activity(
            run_idp,
            request,
            start_to_close_timeout=_IDP_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._status = "registering"
        registered = await workflow.execute_activity(
            register_ingest,
            args=[request, idp],
            start_to_close_timeout=_DB_TIMEOUT,
            retry_policy=_RETRY,
        )

        # The PII gate runs before anything routes the document to a human (INV-7). It writes
        # the verdict onto the version, so publication is impossible until it says `clear`.
        pii: PiiOutcome | None = None
        if not registered.duplicate:
            self._status = "pii_scan"
            pii = await workflow.execute_activity(
                scan_pii,
                args=[registered, idp],
                start_to_close_timeout=_PII_TIMEOUT,
                retry_policy=_RETRY,
            )

        if not registered.duplicate and idp.detected_refs:
            # Unresolved targets are surfaced on the review task rather than dropped: "cites
            # something we do not hold" is information the steward needs.
            unresolved = await workflow.execute_activity(
                link_detected_refs,
                args=[registered.document_id, idp.detected_refs],
                start_to_close_timeout=_DB_TIMEOUT,
                retry_policy=_RETRY,
            )
        else:
            unresolved = []

        decision = decide(idp, registered, pii)
        task_id: str | None = None
        if decision.task is not None:
            if unresolved:
                decision.task.payload["unresolved_references"] = unresolved
            task_id = await workflow.execute_activity(
                open_review_task,
                decision.task,
                start_to_close_timeout=_DB_TIMEOUT,
                retry_policy=_RETRY,
            )

        self._status = decision.status
        return IngestOutcome(
            status=decision.status,
            document_id=registered.document_id,
            version_id=registered.version_id,
            review_task_id=task_id,
            detail=decision.detail,
        )
