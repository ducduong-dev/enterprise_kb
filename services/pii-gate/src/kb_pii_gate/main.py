"""ASGI entrypoint for kb-pii-gate.

Two endpoints, one detector (INV-7):

* `/v1/scan` — the ingestion gate. Called by the Temporal activity before a version can
  become publishable. Fails closed: an incomplete scan is not a clear one.
* `/v1/filter` — the chatbot's output filter. Called by chat-api on every answer before it
  reaches a user (M6), and by the external surface without exception (M8).

They share an implementation deliberately. If the two could drift, an answer could disclose
exactly what ingestion refused to publish, and that gap is the first thing an attacker would
look for.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI
from kb_common.app import create_app
from kb_common.config import get_settings
from kb_common.logging import get_logger
from kb_ports.adapters.generation import OpenAiCompatibleGeneration
from kb_ports.models import PiiDetectorPort
from kb_schemas.kbdoc import KBDoc
from pydantic import BaseModel, Field

from kb_pii_gate.detector import LlmPiiDetector, PatternPiiDetector, scan_document

log = get_logger(__name__)

app: FastAPI = create_app("kb-pii-gate")


def detector() -> PiiDetectorPort:
    """Patterns always; the model as well when one is configured.

    In `test` there is no model, and the pattern rules alone are the gate — which is also the
    production fallback when the model is unreachable, because a gate that stops working when
    a GPU is busy is not a gate.
    """
    settings = get_settings()
    if settings.env == "test":
        return PatternPiiDetector()
    return LlmPiiDetector(OpenAiCompatibleGeneration(settings.models))


class ScanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    kbdoc: KBDoc


class Finding(BaseModel):
    kind: str
    #: Never the matched value itself. A gate that echoes the card number it found has
    #: published the card number to its own logs and to every caller.
    redacted: str
    confidence: float
    detector: str
    block_id: str | None = None


class ScanResponse(BaseModel):
    clear: bool
    scan_complete: bool
    findings: list[Finding] = Field(default_factory=list)
    blocked_blocks: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class FilterRequest(BaseModel):
    model_config = {"extra": "forbid"}

    text: str
    #: The surface asking. `external` never returns unredacted text under any circumstance.
    surface: str = "internal"


class FilterResponse(BaseModel):
    text: str
    redacted: bool
    findings: list[Finding] = Field(default_factory=list)


@app.post("/v1/scan", response_model=ScanResponse)
def scan(request: ScanRequest, gate: PiiDetectorPort = Depends(detector)) -> ScanResponse:
    """Ingestion gate. A version cannot become publishable until this returns `clear`."""
    result = scan_document(gate, request.kbdoc)
    log.info("pii_gate_scan", extra=result.summary())
    return ScanResponse(
        clear=result.is_clear,
        scan_complete=result.result.scan_complete,
        findings=[_finding(f) for f in result.result.findings],
        blocked_blocks=list(result.blocked_blocks),
        summary=result.summary(),
    )


@app.post("/v1/filter", response_model=FilterResponse)
def filter_output(
    request: FilterRequest, gate: PiiDetectorPort = Depends(detector)
) -> FilterResponse:
    """Chat output filter. Redacts rather than refuses: an answer with an identifier removed
    is still an answer, and refusing outright teaches users to ask elsewhere."""
    text, result = gate.redact(request.text)
    if result.findings:
        log.warning(
            "pii_filtered_from_answer",
            extra={
                "surface": request.surface,
                "findings": len(result.findings),
                "kinds": sorted({finding.kind for finding in result.findings}),
            },
        )
    return FilterResponse(
        text=text,
        redacted=bool(result.findings),
        findings=[_finding(finding) for finding in result.findings],
    )


def _finding(finding: Any) -> Finding:
    return Finding(
        kind=finding.kind,
        # The kind and the location, never the value.
        redacted=f"[{finding.kind.upper()}]",
        confidence=finding.confidence,
        detector=finding.detector,
        block_id=finding.block_id,
    )


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
