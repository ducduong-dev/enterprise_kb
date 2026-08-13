"""Port DTOs and the base protocol every adapter implements.

INV-12: OCR, VLM, embedding, rerank, generation and PII detection are reached only through
these interfaces. `scripts/check_invariants.py` fails the build if a service imports a model
client (openai, vllm, paddleocr, transformers, boto3 …) outside an adapter
module. Swapping a vendor must be an adapter + config change, never a service change —
that is also what makes the [OPEN] decisions in the plan safe to defer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """Recorded on outputs that feed decisions, so a result can be traced to the exact
    model/version that produced it (needed for IDP reports and PII audit)."""

    name: str
    version: str = "unknown"
    endpoint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Port(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...
