"""PaddleOCR + PP-Structure behind `OcrPort`.

Paddle is imported lazily and the model is loaded once per process: construction downloads and
initialises several hundred megabytes, which must not happen at import time in a service that
may never OCR anything.

Two details this corpus depends on:

* `lang="vi"` — the Vietnamese recognition model. The Latin model reads Vietnamese text
  *almost* correctly, dropping tone marks, which produces plausible-looking wrong words rather
  than obvious failures. That is the worst possible failure mode here, and it is why the
  confidence scorer treats a low diacritic ratio as a page-level red flag.
* PP-Structure for tables. A fee schedule read as loose lines loses the row/column
  relationship that made it an answer.
"""

from __future__ import annotations

from typing import Any

from kb_common.errors import UpstreamError
from kb_common.logging import get_logger

from kb_ports.base import AdapterInfo
from kb_ports.models import OcrLine, OcrPageResult
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

#: PaddleOCR reports confidence per recognized line in [0, 1].
DEFAULT_LANG = "vi"


class PaddleOcrAdapter:
    def __init__(self, lang: str = DEFAULT_LANG, use_structure: bool = True) -> None:
        self._lang = lang
        self._use_structure = use_structure
        self._ocr: Any | None = None
        self._structure: Any | None = None

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="paddleocr",
            version=self._lang,
            extra={"structure": self._use_structure},
        )

    def health(self) -> bool:
        try:
            self._engine()
        except Exception:  # pragma: no cover - depends on the model being installed
            return False
        return True

    def _engine(self) -> Any:
        if self._ocr is None:
            from paddleocr import (
                PaddleOCR,  # imported lazily: loading the model costs hundreds of megabytes
            )

            # angle classification on: pages arrive upside down often enough to matter, and
            # preprocessing only corrects small skew, not a 180° flip.
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self._lang, show_log=False)
        return self._ocr

    def _structure_engine(self) -> Any:  # pragma: no cover - requires the model
        if self._structure is None:
            from paddleocr import (
                PPStructure,  # imported lazily: loading the model costs hundreds of megabytes
            )

            self._structure = PPStructure(show_log=False, lang=self._lang)
        return self._structure

    def recognize_page(self, image: bytes, *, page: int = 1) -> OcrPageResult:
        import cv2  # lazy: only needed on the OCR path
        import numpy as np

        array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            raise UpstreamError("page image could not be decoded", page=page)
        try:
            raw = self._engine().ocr(np.asarray(array), cls=True)
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            raise UpstreamError("OCR engine failed", page=page) from exc

        lines: list[OcrLine] = []
        for entry in raw[0] if raw and raw[0] else []:
            box, (text, confidence) = entry[0], entry[1]
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            lines.append(
                OcrLine(
                    text=str(text),
                    bbox=(float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))),
                    confidence=float(confidence),
                )
            )

        tables: list[list[list[str]]] = []
        layout: list[dict[str, Any]] = []
        if self._use_structure:  # pragma: no cover - requires the model
            tables, layout = self._structure_page(array)

        mean = sum(line.confidence for line in lines) / len(lines) if lines else 0.0
        return OcrPageResult(
            page=page, lines=lines, tables=tables, layout=layout, mean_confidence=mean
        )

    def _structure_page(  # pragma: no cover - requires the model
        self, array: Any
    ) -> tuple[list[list[list[str]]], list[dict[str, Any]]]:
        import numpy as np

        results = self._structure_engine()(np.asarray(array))
        tables: list[list[list[str]]] = []
        layout: list[dict[str, Any]] = []
        for region in results:
            layout.append(
                {
                    "type": region.get("type"),
                    "bbox": [float(v) for v in region.get("bbox", [])],
                }
            )
            if region.get("type") == "table":
                html = (region.get("res") or {}).get("html")
                if html:
                    tables.append(_table_from_html(html))
        return tables, layout


def _table_from_html(html: str) -> list[list[str]]:  # pragma: no cover - requires the model
    """PP-Structure returns tables as HTML; the KBDoc contract wants rows of cells."""
    from html.parser import HTMLParser

    class _Rows(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.rows: list[list[str]] = []
            self._row: list[str] | None = None
            self._cell: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "tr":
                self._row = []
            elif tag in {"td", "th"}:
                self._cell = []

        def handle_endtag(self, tag: str) -> None:
            if tag in {"td", "th"} and self._row is not None:
                self._row.append("".join(self._cell).strip())
            elif tag == "tr" and self._row is not None:
                self.rows.append(self._row)
                self._row = None

        def handle_data(self, data: str) -> None:
            self._cell.append(data)

    parser = _Rows()
    parser.feed(html)
    return parser.rows


@register_adapter(PortName.OCR, "paddle")
def build_paddle_ocr(lang: str = DEFAULT_LANG, use_structure: bool = True) -> PaddleOcrAdapter:
    return PaddleOcrAdapter(lang=lang, use_structure=use_structure)
