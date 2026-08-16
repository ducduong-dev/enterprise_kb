"""`ExpirySweepWorkflow` — the platform's first scheduled job (ADR-0031).

A Temporal Schedule rather than a cron container or a new service: the worker and the schedule
machinery are already deployed, and a workflow gives every run a history somebody can read
when the question is "did it run on the 3rd, and what did it do".

The workflow is thin on purpose. All of the work is one activity over one transaction, because
the sweep has nothing to coordinate — no human step, no model call, no fan-out. What it buys
by being a workflow at all is the run history and the retry policy.

**Nothing in here decides anything.** If this job never runs again, no answer changes: the
expiry dates were projected onto the chunks at confirmation time and the effectivity predicate
reads them on every query. What stops is the relabelling and the queues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from kb_workflows.expiry_activities import SweepOutcome, run_expiry_sweep

#: The sweep is a handful of indexed queries over the whole corpus; generous, not unbounded.
_SWEEP_TIMEOUT = timedelta(minutes=15)

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    # A sweep that cannot run today runs tomorrow, and tomorrow's run covers today's work
    # because it reads a clock rather than a cursor. There is nothing to catch up.
    maximum_attempts=3,
)

#: 02:15 local time: after end-of-day batch, before the working day. The minute is offset from
#: the hour so it does not contend with everything else scheduled on the hour.
SCHEDULE_ID = "kb-expiry-sweep"
SCHEDULE_CRON = "15 2 * * *"


@dataclass
class SweepReport:
    ran_for: str = ""
    expired: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)
    attestations: list[str] = field(default_factory=list)


@workflow.defn(name="ExpirySweepWorkflow")
class ExpirySweepWorkflow:
    @workflow.run
    async def run(self, as_of: str | None = None) -> SweepReport:
        outcome: SweepOutcome = await workflow.execute_activity(
            run_expiry_sweep,
            as_of,
            start_to_close_timeout=_SWEEP_TIMEOUT,
            retry_policy=_RETRY,
        )
        return SweepReport(
            ran_for=outcome.ran_for,
            expired=outcome.expired,
            warned=outcome.warned,
            attestations=outcome.attestations,
        )


async def ensure_schedule(client: object) -> str:  # pragma: no cover - needs a live Temporal
    """Create the daily schedule, or leave the existing one alone.

    Idempotent so it can run on every worker start: a deployment must not silently reset a
    schedule an operator paused during an incident.
    """
    from kb_common.config import get_settings
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleSpec,
    )

    assert isinstance(client, Client)
    settings = get_settings()
    try:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ExpirySweepWorkflow.run,
                    args=[None],
                    id=f"{SCHEDULE_ID}-run",
                    task_queue=settings.temporal.task_queue,
                ),
                spec=ScheduleSpec(cron_expressions=[SCHEDULE_CRON]),
            ),
        )
        return "created"
    except ScheduleAlreadyRunningError:
        return "exists"


__all__ = ["SCHEDULE_CRON", "SCHEDULE_ID", "ExpirySweepWorkflow", "SweepReport", "ensure_schedule"]
