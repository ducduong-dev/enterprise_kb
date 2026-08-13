"""PDF with a text layer → KBDoc.

A PDF without a usable text layer is not an error, it is a *different route*: this parser
raises `RequiresOcr`, and `IngestWorkflow` sends the document down the OCR chain (M3). Making
that an explicit signal rather than "returned almost no text" means a scanned document can
never quietly become a nearly-empty KBDoc that publishes fine and answers nothing.

Bounding boxes are kept per block: the M3 review editor jumps from a block to its place on the
page image, and that only works if the coordinates survive from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymupdf
from kb_common.errors import KBError
from kb_schemas.kbdoc import KBDoc

from kb_idp.builder import KBDocBuilder

#: Below this many characters per page on average, the text layer is decorative (a scanner
#: watermark, a footer) and the real content is in the images.
_MIN_CHARS_PER_PAGE = 40


class RequiresOcr(KBError):
    """The document has no usable text layer — route it to the OCR chain."""

    status_code = 422
    code = "requires_ocr"
    public = True


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool


def parse_pdf_native(data: bytes) -> KBDoc:
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        pages = [_read_page(document[index]) for index in range(document.page_count)]
        total_chars = sum(len(line.text) for page in pages for line in page)
        if document.page_count and total_chars / document.page_count < _MIN_CHARS_PER_PAGE:
            raise RequiresOcr(
                "PDF has no usable text layer",
                page_count=document.page_count,
                chars=total_chars,
            )

        builder = KBDocBuilder(source_format="pdf_native")
        builder.note_engine("pymupdf", pymupdf.__version__)

        body_size = _median_size([line for page in pages for line in page])
        for page_number, lines in enumerate(pages, start=1):
            tables = _extract_tables(document[page_number - 1])
            for rows in tables:
                builder.add_table(rows, header_rows=1, page=page_number)
            for line in lines:
                builder.add(
                    line.text,
                    # Larger or bold text is a heading candidate; the section tracker still
                    # has the final say via "Điều 12"-style matching.
                    block_type="heading"
                    if (line.size > body_size * 1.15 or line.bold) and len(line.text) < 120
                    else None,
                    page=page_number,
                    bbox=line.bbox,
                )

        metadata = document.metadata or {}
        if metadata.get("title"):
            builder.set_title_hint(str(metadata["title"]))
        return builder.build(page_count=document.page_count, source_format="pdf_native")
    finally:
        document.close()


def _read_page(page: Any) -> list[_Line]:
    lines: list[_Line] = []
    content = page.get_text("dict")
    for block in content.get("blocks", []):
        if block.get("type") != 0:  # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans)
            if not text.strip():
                continue
            sizes = [float(span.get("size", 0)) for span in spans] or [0.0]
            flags = [int(span.get("flags", 0)) for span in spans]
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            lines.append(
                _Line(
                    text=text,
                    bbox=bbox,  # type: ignore[arg-type]
                    size=max(sizes),
                    bold=any(flag & 2**4 for flag in flags),  # bit 4 = bold
                )
            )
    return lines


def _extract_tables(page: Any) -> list[list[list[str]]]:
    """PyMuPDF's table finder. Best-effort: a missed table still yields its text as lines."""
    finder = getattr(page, "find_tables", None)
    if finder is None:  # pragma: no cover - depends on PyMuPDF version
        return []
    try:
        found = finder()
    except Exception:  # pragma: no cover - defensive, table finding is heuristic
        return []
    tables: list[list[list[str]]] = []
    for table in getattr(found, "tables", []):
        rows = [[(cell or "").strip() for cell in row] for row in table.extract()]
        if rows:
            tables.append(rows)
    return tables


def _median_size(lines: list[_Line]) -> float:
    sizes = sorted(line.size for line in lines)
    if not sizes:
        return 0.0
    return sizes[len(sizes) // 2]
