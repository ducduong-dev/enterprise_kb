"""The PII detector: deterministic rules, plus an LLM for what rules cannot see.

Two call sites, one implementation, deliberately (INV-7): the ingestion gate and the chatbot's
output filter. If they could differ, an answer could leak precisely what ingestion refused to
publish — and that gap is the one an attacker would look for first.

Composition rules, in order of importance:

1. **Patterns are the floor.** A pattern match blocks. The model cannot un-block it: a
   prompt-injected document must not be able to talk its way past a Luhn-valid card number.
2. **The model can only add.** It exists for the paragraph that identifies one customer
   without quoting an identifier, which no regex will ever catch.
3. **Fail closed.** A model that times out, returns unparseable output, or is not configured
   leaves the scan *incomplete*, and an incomplete scan is not a clear one. The version stays
   `pending` and publication stays impossible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from kb_common.logging import get_logger
from kb_ports.base import AdapterInfo
from kb_ports.models import (
    Generation,
    GenerationPort,
    Message,
    PiiDetectorPort,
    PiiFinding,
    PiiScanResult,
)
from kb_ports.registry import PortName, register_adapter
from kb_schemas.kbdoc import KBDoc

from kb_pii_gate.patterns import redact_text, scan_text

log = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "pii_detector.md"
PROMPT_VERSION = "1"

#: Below this the model's judgement is noise; it is recorded but does not block on its own.
LLM_MIN_CONFIDENCE = 0.6
#: Long documents are judged in windows: a model's attention to a single sentence degrades
#: across a 40-page circular, and a per-window verdict also localizes the finding for review.
WINDOW_CHARS = 3000
WINDOW_OVERLAP = 200

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class ScanSummary(TypedDict):
    """What the gate reports about a document — kinds and counts, never the values."""

    clear: bool
    scan_complete: bool
    finding_count: int
    kinds: dict[str, int]
    blocks: list[str]


@dataclass(frozen=True, slots=True)
class DocumentScan:
    """A whole-document verdict, which is what the gate acts on."""

    result: PiiScanResult
    #: Block ids that carry findings — what the review editor highlights.
    blocked_blocks: tuple[str, ...] = ()

    @property
    def is_clear(self) -> bool:
        return self.result.is_clear

    def summary(self) -> ScanSummary:
        kinds: dict[str, int] = {}
        for finding in self.result.findings:
            kinds[finding.kind] = kinds.get(finding.kind, 0) + 1
        return ScanSummary(
            clear=self.is_clear,
            scan_complete=self.result.scan_complete,
            finding_count=len(self.result.findings),
            kinds=kinds,
            blocks=list(self.blocked_blocks),
        )


class PatternPiiDetector:
    """Rules only. The gate's hard floor, and the whole detector when no model is available."""

    @property
    def info(self) -> AdapterInfo:
        # `real_rules` is what the external surface checks before it will answer at all
        # (INV-7): a stand-in detector that declares False cannot serve the public.
        return AdapterInfo(name="patterns", version="1", extra={"llm": False, "real_rules": True})

    def health(self) -> bool:
        return True

    def scan(self, text: str, *, block_id: str | None = None) -> PiiScanResult:
        return PiiScanResult(findings=scan_text(text, block_id=block_id), scan_complete=True)

    def redact(self, text: str) -> tuple[str, PiiScanResult]:
        result = self.scan(text)
        return redact_text(text, result.findings), result


class LlmPiiDetector:
    """Patterns plus a model, for PII that has no shape.

    The model is asked for a JSON verdict and nothing else; anything it returns that cannot be
    parsed makes the scan incomplete rather than clear (INV-7). It is never given the power to
    contradict a pattern.
    """

    def __init__(
        self,
        generation: GenerationPort,
        *,
        prompt_path: Path | None = None,
        window_chars: int = WINDOW_CHARS,
    ) -> None:
        self._generation = generation
        self._prompt = (prompt_path or PROMPT_PATH).read_text(encoding="utf-8")
        self._window = window_chars

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="patterns+llm",
            version=PROMPT_VERSION,
            extra={"llm": True, "real_rules": True, "model": self._generation.info.version},
        )

    def health(self) -> bool:
        return self._generation.health()

    def scan(self, text: str, *, block_id: str | None = None) -> PiiScanResult:
        findings = scan_text(text, block_id=block_id)
        complete = True

        for offset, window in _windows(text, self._window):
            try:
                verdict = self._judge(window)
            except Exception as exc:
                log.warning("pii_llm_unavailable", extra={"error": str(exc), "block_id": block_id})
                complete = False
                continue
            findings.extend(_findings_from(verdict, window, offset, block_id))

        return PiiScanResult(findings=findings, scan_complete=complete)

    def redact(self, text: str) -> tuple[str, PiiScanResult]:
        result = self.scan(text)
        return redact_text(text, result.findings), result

    def _judge(self, window: str) -> dict[str, Any]:
        prompt = self._prompt.replace("{text}", window)
        response: Generation = self._generation.generate(
            [Message(role="user", content=prompt)], temperature=0.0, max_tokens=512
        )
        match = _JSON.search(response.text)
        if match is None:
            raise ValueError("model did not return JSON")
        parsed: dict[str, Any] = json.loads(match.group(0))
        return parsed


def _windows(text: str, size: int) -> list[tuple[int, str]]:
    if len(text) <= size:
        return [(0, text)]
    windows: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        windows.append((start, text[start : start + size]))
        start += size - WINDOW_OVERLAP
    return windows


def _findings_from(
    verdict: dict[str, Any], window: str, offset: int, block_id: str | None
) -> list[PiiFinding]:
    if not verdict.get("has_pii"):
        return []
    confidence = float(verdict.get("confidence", 0.0) or 0.0)
    if confidence < LLM_MIN_CONFIDENCE:
        return []

    findings: list[PiiFinding] = []
    for item in verdict.get("findings", []) or []:
        quote = str(item.get("quote", ""))
        # Locate the quote in the source so the reviewer sees where it is; a model quoting
        # something not in the text is a hallucination, and its span is not trusted.
        position = window.find(quote) if quote else -1
        findings.append(
            PiiFinding(
                kind=str(item.get("kind", "identifiable_individual")),
                text=quote,
                start=offset + position if position >= 0 else offset,
                end=offset + position + len(quote) if position >= 0 else offset,
                confidence=confidence,
                detector="llm",
                block_id=block_id,
            )
        )
    return findings or [
        PiiFinding(
            kind="identifiable_individual",
            text="",
            start=offset,
            end=offset,
            confidence=confidence,
            detector="llm",
            block_id=block_id,
        )
    ]


def scan_document(detector: PiiDetectorPort, kbdoc: KBDoc) -> DocumentScan:
    """Scan every block of a parsed document.

    Block by block rather than on the concatenated text: the review editor highlights the
    block that carries the finding, and a reviewer told only "this document contains a card
    number" has to read the whole thing to find it.
    """
    findings: list[PiiFinding] = []
    complete = True
    blocked: list[str] = []

    for block in kbdoc.blocks:
        if not block.text.strip():
            continue
        result = detector.scan(block.text, block_id=block.id)
        complete = complete and result.scan_complete
        if result.findings:
            blocked.append(block.id)
            findings.extend(result.findings)

    scan = PiiScanResult(findings=findings, scan_complete=complete)
    log.info(
        "pii_document_scanned",
        extra={
            "blocks": len(kbdoc.blocks),
            "findings": len(findings),
            "blocked_blocks": len(blocked),
            "scan_complete": complete,
            "detector": detector.info.name,
        },
    )
    return DocumentScan(result=scan, blocked_blocks=tuple(blocked))


@register_adapter(PortName.PII, "patterns")
def build_pattern_detector() -> PatternPiiDetector:
    return PatternPiiDetector()


@register_adapter(PortName.PII, "llm")
def build_llm_detector(generation: GenerationPort) -> LlmPiiDetector:
    return LlmPiiDetector(generation)
