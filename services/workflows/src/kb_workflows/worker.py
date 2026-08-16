"""Temporal worker entrypoint.

Workflows are deterministic; every side effect (IDP, PII scan, publish, index write) is an
activity. Human steps (review decisions, approvals) arrive as signals, so a review that takes
three weeks costs nothing while it waits.
"""

from __future__ import annotations

from kb_common.config import get_settings
from kb_common.logging import configure_logging, get_logger

log = get_logger(__name__)


async def run() -> None:  # pragma: no cover - process entrypoint
    from temporalio.client import Client
    from temporalio.worker import Worker

    from kb_workflows.activities import INGEST_ACTIVITIES
    from kb_workflows.expiry_activities import EXPIRY_ACTIVITIES
    from kb_workflows.expiry_sweep import ExpirySweepWorkflow, ensure_schedule
    from kb_workflows.ingest import IngestWorkflow
    from kb_workflows.merge import ConsolidationWorkflow, MergeFlow
    from kb_workflows.merge_activities import MERGE_ACTIVITIES

    settings = get_settings()
    configure_logging("kb-workflows", settings.log_level, settings.log_format)
    client = await Client.connect(settings.temporal.host, namespace=settings.temporal.namespace)
    activities = [*INGEST_ACTIVITIES, *MERGE_ACTIVITIES, *EXPIRY_ACTIVITIES]
    log.info(
        "worker_starting",
        extra={
            "task_queue": settings.temporal.task_queue,
            "workflows": [
                "IngestWorkflow",
                "MergeFlow",
                "ConsolidationWorkflow",
                "ExpirySweepWorkflow",
            ],
            "activities": [a.__name__ for a in activities],
        },
    )
    # Idempotent, and deliberately not a deployment step somebody has to remember: a corpus
    # whose expiries are confirmed but never relabelled looks fine until somebody asks why
    # nothing is ever `expired`.
    log.info("expiry_schedule", extra={"result": await ensure_schedule(client)})
    worker = Worker(
        client,
        task_queue=settings.temporal.task_queue,
        workflows=[IngestWorkflow, MergeFlow, ConsolidationWorkflow, ExpirySweepWorkflow],
        activities=activities,
    )
    await worker.run()


def main() -> None:  # pragma: no cover - process entrypoint
    import asyncio

    asyncio.run(run())
