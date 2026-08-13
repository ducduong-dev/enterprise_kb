"""Vision transcription — a VLM reading a scanned page, and its recorded counterpart.

The VLM is the second opinion on pages PaddleOCR could not read: heavy stamps over text,
handwritten annotations, degraded photocopies, complex nested tables. It is perhaps two orders
of magnitude more expensive per page, so it runs on the pages the confidence scorer flags, not
on the corpus.

The prompt is the interesting part. A VLM asked to "read this page" will happily *improve* it —
fix a typo, complete a truncated sentence, translate a heading. For a bank's regulatory archive
that is corruption, not help, so the instruction is explicit about transcribing rather than
interpreting, and about preserving Vietnamese diacritics exactly.

Which model reads the page is a route, not a class (ADR-0024): local Qwen2.5-VL through vLLM,
or a public vision model (Gemini, GPT-4o, Claude) through the same LiteLLM proxy — the request
is identical, because they all speak OpenAI chat-completions with an image part. What changes
is that the page image leaves the bank, which `kb_ports.proxy.route` refuses until Compliance
has ruled ([OPEN]-1, ADR-0025) and which every transcription records in `info`.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from kb_common.config import ModelGatewaySettings, get_settings
from kb_common.errors import NotFound, UpstreamError
from kb_common.logging import get_logger

from kb_ports.base import AdapterInfo
from kb_ports.models import OcrLine, OcrPageResult
from kb_ports.proxy import route
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

TRANSCRIBE_PROMPT = """Bạn là công cụ chuyển văn bản từ ảnh quét sang văn bản số.

Nhiệm vụ: chép lại CHÍNH XÁC toàn bộ nội dung chữ trong ảnh, theo đúng thứ tự đọc.

Quy tắc bắt buộc:
- Giữ nguyên dấu tiếng Việt. Không bỏ dấu, không đổi chữ.
- Không sửa lỗi chính tả, không diễn giải, không tóm tắt, không dịch.
- Giữ nguyên số hiệu văn bản, số liệu, ngày tháng đúng như trong ảnh.
- Mỗi dòng trong ảnh là một dòng trong kết quả.
- Bảng: mỗi hàng một dòng, các ô cách nhau bằng dấu " | ".
- Nếu một phần không đọc được, ghi [không đọc được] ở đúng vị trí đó.
- Không thêm bất kỳ lời giải thích nào ngoài nội dung được chép."""

#: Deterministic transcription. Any sampling temperature turns "read this" into "write
#: something plausible", which is precisely the failure this prompt guards against.
TEMPERATURE = 0.0
#: A dense A4 page of Vietnamese runs to roughly 3k tokens; leave headroom for tables.
MAX_TOKENS = 4096
#: A transcription is not a confidence-bearing output the way OCR is. This is the value the
#: pipeline attributes to VLM lines: better than the OCR it replaced, but still machine output
#: that a reviewer should look at (it sits below `LOW_CONFIDENCE_THRESHOLD`).
VLM_LINE_CONFIDENCE = 0.82


class VisionAdapter:
    """Any vision model that speaks OpenAI chat-completions: local vLLM, or public via proxy."""

    def __init__(
        self,
        settings: ModelGatewaySettings | None = None,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ):
        self._cfg = settings or get_settings().models
        self._route = route("vlm", self._cfg)
        self._client = client or httpx.Client(
            base_url=self._route.base_url, timeout=timeout, headers=self._route.headers
        )

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="vision",
            version=self._route.model,
            endpoint=self._route.base_url,
            # A page image is the document itself. Whether it left the bank to be read is the
            # first thing an auditor asks about a transcription, so it travels with it.
            extra={
                "leaves_network": self._route.leaves_network,
                "proxied": self._route.proxied,
                "real_vlm": True,
            },
        )

    def health(self) -> bool:
        try:
            # The proxy answers /health; a bare vLLM does too. Either way an unhealthy vision
            # model means scans get queued rather than silently transcribed by nothing.
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:  # pragma: no cover - network dependent
            return False

    def transcribe_page(self, image: bytes, *, prompt: str | None = None) -> OcrPageResult:
        encoded = base64.b64encode(image).decode("ascii")
        payload: dict[str, Any] = {
            "model": self._route.model,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or TRANSCRIBE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                }
            ],
        }
        try:
            response = self._client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("VLM transcription failed", model=self._route.model) from exc

        content = response.json()["choices"][0]["message"]["content"]
        return _result_from_text(content)


def _result_from_text(content: str, page: int = 1) -> OcrPageResult:
    """Turn a transcription into lines.

    The VLM returns text without coordinates, so blocks from an escalated page have no bbox.
    The review editor shows the page image beside them and says why: a reviewer checking an
    escalated page is reading the whole page anyway.
    """
    lines = [
        OcrLine(text=line.strip(), bbox=(0.0, 0.0, 0.0, 0.0), confidence=VLM_LINE_CONFIDENCE)
        for line in content.splitlines()
        if line.strip()
    ]
    tables = [
        [cell.strip() for cell in line.text.split("|")]
        for line in lines
        if line.text.count("|") >= 2
    ]
    return OcrPageResult(
        page=page,
        lines=lines,
        tables=[tables] if tables else [],
        mean_confidence=VLM_LINE_CONFIDENCE if lines else 0.0,
    )


@dataclass
class RecordedVlmAdapter:
    """Replays recorded transcriptions, keyed by page-image hash. See `ocr_recorded`."""

    recordings: dict[str, str] = field(default_factory=dict)
    strict: bool = True

    @classmethod
    def from_directory(cls, directory: Path, *, strict: bool = True) -> RecordedVlmAdapter:
        recordings: dict[str, str] = {}
        for path in sorted(directory.glob("*.vlm.json")):
            recordings.update(json.loads(path.read_text(encoding="utf-8")))
        return cls(recordings=recordings, strict=strict)

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="recorded-vlm",
            version="1",
            extra={"real_vlm": False, "pages": len(self.recordings)},
        )

    def health(self) -> bool:
        return True

    def transcribe_page(self, image: bytes, *, prompt: str | None = None) -> OcrPageResult:
        from kb_ports.adapters.ocr_recorded import image_key

        key = image_key(image)
        content = self.recordings.get(key)
        if content is None:
            if self.strict:
                raise NotFound("no VLM recording for this page image", image_key=key)
            return OcrPageResult(page=1, lines=[], mean_confidence=0.0)
        return _result_from_text(content)


@register_adapter(PortName.VLM, "vision")
def build_vision(settings: ModelGatewaySettings | None = None) -> VisionAdapter:
    """The configured vision model, wherever `kb_ports.proxy.route` says it lives."""
    return VisionAdapter(settings)


@register_adapter(PortName.VLM, "recorded")
def build_recorded_vlm(directory: Path | str, strict: bool = True) -> RecordedVlmAdapter:
    return RecordedVlmAdapter.from_directory(Path(directory), strict=strict)
