"""Model ports: embedding, rerank, generation, OCR, VLM, PII detection (INV-12).

Every one of these has at least a local adapter. `GenerationPort` has two production
adapters (vLLM and hosted API) because [OPEN]-1 is unresolved; the default is local, and
switching is a config change with no service code touched.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kb_ports.base import AdapterInfo

# ------------------------------------------------------------------------ embedding/rerank


class EmbeddingPort(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    @property
    def dimensions(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
        """Separate from `embed_documents`: BGE-M3 query prefixes differ from passage ones,
        and getting this wrong silently degrades recall rather than failing."""
        ...


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


class RerankPort(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def rerank(
        self, query: str, passages: Sequence[str], *, top_k: int | None = None
    ) -> list[RerankResult]: ...


# ---------------------------------------------------------------------------- generation


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Generation:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    model: str = "unknown"


class GenerationPort(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def generate(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        stop: Sequence[str] | None = None,
    ) -> Generation: ...

    def stream(
        self, messages: Sequence[Message], *, temperature: float = 0.0, max_tokens: int = 1024
    ) -> Iterator[str]: ...


# ----------------------------------------------------------------------------- OCR / VLM


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float


@dataclass(slots=True)
class OcrPageResult:
    page: int
    lines: list[OcrLine] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    layout: list[dict[str, Any]] = field(default_factory=list)
    mean_confidence: float = 0.0


class OcrPort(Protocol):
    """PaddleOCR + PP-Structure behind this. Vietnamese diacritics must survive verbatim —
    the M3 fixture test compares bytes, not normalized forms."""

    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def recognize_page(self, image: bytes, *, page: int = 1) -> OcrPageResult: ...


class VlmPort(Protocol):
    """Escalation path for pages the OCR chain is not confident about."""

    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def transcribe_page(self, image: bytes, *, prompt: str | None = None) -> OcrPageResult: ...


# ------------------------------------------------------------------------------ PII gate


@dataclass(frozen=True, slots=True)
class PiiFinding:
    kind: str  # account_number | cccd | pan | name_with_balance | phone | email | address
    text: str
    start: int
    end: int
    confidence: float
    detector: str  # "pattern:pan_luhn" | "llm"
    block_id: str | None = None


@dataclass(slots=True)
class PiiScanResult:
    findings: list[PiiFinding] = field(default_factory=list)
    #: Fail closed (INV-7): an inconclusive scan is *not* clear.
    scan_complete: bool = True

    @property
    def is_clear(self) -> bool:
        return self.scan_complete and not self.findings


class PiiDetectorPort(Protocol):
    """One implementation, two call sites: the ingestion gate and the chatbot output filter.
    They must share a detector so an answer cannot leak what ingestion would have blocked."""

    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def scan(self, text: str, *, block_id: str | None = None) -> PiiScanResult: ...

    def redact(self, text: str) -> tuple[str, PiiScanResult]: ...
