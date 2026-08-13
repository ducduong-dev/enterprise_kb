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
    from kb_workflows.ingest import IngestWorkflow
    from kb_workflows.merge import ConsolidationWorkflow, MergeFlow
    from kb_workflows.merge_activities import MERGE_ACTIVITIES

    settings = get_settings()
    configure_logging("kb-workflows", settings.log_level, settings.log_format)
    client = await Client.connect(settings.temporal.host, namespace=settings.temporal.namespace)
    log.info(
        "worker_starting",
        extra={
            "task_queue": settings.temporal.task_queue,
            "workflows": ["IngestWorkflow", "MergeFlow", "ConsolidationWorkflow"],
            "activities": [a.__name__ for a in (*INGEST_ACTIVITIES, *MERGE_ACTIVITIES)],
        },
    )
    worker = Worker(
        client,
        task_queue=settings.temporal.task_queue,
        workflows=[IngestWorkflow, MergeFlow, ConsolidationWorkflow],
        activities=[*INGEST_ACTIVITIES, *MERGE_ACTIVITIES],
    )
    await worker.run()


def main() -> None:  # pragma: no cover - process entrypoint
    import asyncio

    asyncio.run(run())
