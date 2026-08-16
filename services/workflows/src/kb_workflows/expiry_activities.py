"""The sweep's one activity.

Everything the sweep does is database work in one transaction, so there is one activity and it
delegates to `kb_registry.sweep.ExpirySweep`. The logic lives in the registry rather than here
for the ordinary reason: it is a registry rule, and it has to be testable against a database
without a Temporal worker in the way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from kb_common.audit import SqlAuditSink
from kb_common.db import session_scope
from kb_common.logging import get_logger
from kb_registry.sweep import ExpirySweep
from temporalio import activity

log = get_logger(__name__)


@dataclass
class SweepOutcome:
    ran_for: str = ""
    expired: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)
    attestations: list[str] = field(default_factory=list)


@activity.defn(name="run_expiry_sweep")
async def run_expiry_sweep(as_of: str | None = None) -> SweepOutcome:
    """Relabel, warn, attest. Never decide.

    `as_of` exists for replaying a day during an investigation and for tests that freeze the
    clock; the schedule always passes `None`, which means today.
    """
    when = date.fromisoformat(as_of) if as_of else None
    with session_scope() as session:
        result = ExpirySweep(session, audit=SqlAuditSink(session)).run(today=when)

    log.info("expiry_sweep_completed", extra=result.as_dict())
    return SweepOutcome(
        ran_for=result.ran_for.isoformat(),
        expired=[str(item) for item in result.expired],
        warned=[str(item) for item in result.warned],
        attestations=[str(item) for item in result.attestations],
    )


EXPIRY_ACTIVITIES: Sequence[Callable[..., Any]] = [run_expiry_sweep]

__all__ = ["EXPIRY_ACTIVITIES", "SweepOutcome", "run_expiry_sweep"]
