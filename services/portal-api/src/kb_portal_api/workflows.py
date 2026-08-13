"""Starting and querying Temporal workflows.

Behind a small protocol so the API can be tested without a Temporal server, and so the portal
never imports the Temporal client directly — the same reasoning as the model ports (INV-12).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from kb_common.config import get_settings
from kb_common.errors import NotFound, UpstreamError
from kb_common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkflowHandle:
    workflow_id: str
    run_id: str = ""


class WorkflowStarter(Protocol):
    async def start_ingest(self, request: Any, *, workflow_id: str) -> WorkflowHandle: ...

    async def status(self, workflow_id: str) -> str: ...

    async def result(self, workflow_id: str) -> dict[str, Any] | None: ...

    async def signal(self, workflow_id: str, name: str, **payload: Any) -> bool: ...


class TemporalStarter:
    def __init__(self) -> None:
        self._settings = get_settings().temporal
        self._client: Any | None = None

    async def _connect(self) -> Any:
        if self._client is None:
            from temporalio.client import Client

            self._client = await Client.connect(
                self._settings.host, namespace=self._settings.namespace
            )
        return self._client

    async def start_ingest(self, request: Any, *, workflow_id: str) -> WorkflowHandle:
        from kb_workflows.ingest import IngestWorkflow

        client = await self._connect()
        try:
            handle = await client.start_workflow(
                IngestWorkflow.run,
                request,
                id=workflow_id,
                task_queue=self._settings.task_queue,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            raise UpstreamError("could not start the ingest workflow") from exc
        log.info("ingest_workflow_started", extra={"workflow_id": workflow_id})
        return WorkflowHandle(workflow_id=workflow_id, run_id=handle.result_run_id or "")

    async def status(self, workflow_id: str) -> str:
        client = await self._connect()
        handle = client.get_workflow_handle(workflow_id)
        try:
            return str(await handle.query("status"))
        except Exception as exc:  # pragma: no cover - network dependent
            raise NotFound("unknown workflow", workflow_id=workflow_id) from exc

    async def signal(self, workflow_id: str, name: str, **payload: Any) -> bool:
        """Deliver a human decision to a waiting workflow.

        A merge approval is already recorded in the database before this runs, so a failure
        here loses the *delivery*, not the decision: the reviewer is told it was not delivered
        and the workflow can be signalled again.
        """
        client = await self._connect()
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(name, **payload)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning(
                "workflow_signal_failed",
                extra={"workflow_id": workflow_id, "signal": name, "error": str(exc)},
            )
            return False
        return True

    async def result(self, workflow_id: str) -> dict[str, Any] | None:
        client = await self._connect()
        handle = client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        if description.status is None or description.status.name != "COMPLETED":
            return None
        return _as_dict(await handle.result())


def _as_dict(outcome: Any) -> dict[str, Any]:
    """Whatever Temporal handed back, as the portal's JSON body.

    A client that declares no `result_type` gets the payload decoded as plain JSON — a dict —
    not the workflow's dataclass. `vars()` on that raises, and only against a real Temporal:
    the in-memory double returns dicts, so every test passed while the upload screen 500'd.
    """
    if isinstance(outcome, Mapping):
        return dict(outcome)
    return dict(vars(outcome))


@dataclass
class InMemoryStarter:
    """Test double. Records what would have been started and reports a fixed status."""

    started: list[tuple[str, Any]] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    signals: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    async def start_ingest(self, request: Any, *, workflow_id: str) -> WorkflowHandle:
        self.started.append((workflow_id, request))
        self.statuses.setdefault(workflow_id, "parsing")
        return WorkflowHandle(workflow_id=workflow_id, run_id=uuid.uuid4().hex)

    async def status(self, workflow_id: str) -> str:
        if workflow_id not in self.statuses:
            raise NotFound("unknown workflow", workflow_id=workflow_id)
        return self.statuses[workflow_id]

    async def signal(self, workflow_id: str, name: str, **payload: Any) -> bool:
        if workflow_id not in self.statuses:
            return False
        self.signals.append((workflow_id, name, payload))
        return True

    async def result(self, workflow_id: str) -> dict[str, Any] | None:
        return self.results.get(workflow_id)
