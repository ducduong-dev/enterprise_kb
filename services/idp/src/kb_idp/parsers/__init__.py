"""Format → parser routing.

Adding a format is adding an entry here and a module beside it. Everything downstream depends
only on `KBDoc`, so a new source format is invisible to the registry, the chunker and the
chatbots — that is the point of the contract.
"""

from __future__ import annotations

from collections.abc import Callable

from kb_schemas.kbdoc import KBDoc, SourceFormat

from kb_idp.detect import UnsupportedFormat
from kb_idp.parsers.docx import parse_docx
from kb_idp.parsers.pdf_native import RequiresOcr, parse_pdf_native
from kb_idp.parsers.pptx import parse_pptx
from kb_idp.parsers.text import parse_html, parse_txt
from kb_idp.parsers.xlsx import parse_xlsx

Parser = Callable[[bytes], KBDoc]

PARSERS: dict[str, Parser] = {
    "docx": parse_docx,
    "xlsx": parse_xlsx,
    "pptx": parse_pptx,
    "pdf_native": parse_pdf_native,
    "html": parse_html,
    "txt": parse_txt,
}

#: Formats that only the M3 OCR chain can handle.
OCR_FORMATS: frozenset[str] = frozenset({"pdf_scanned", "image"})


def get_parser(source_format: SourceFormat) -> Parser:
    if source_format in OCR_FORMATS:
        raise RequiresOcr("format requires the OCR chain", source_format=source_format)
    try:
        return PARSERS[source_format]
    except KeyError as exc:
        raise UnsupportedFormat(
            f"no parser for {source_format!r}", supported=sorted(PARSERS)
        ) from exc


__all__ = [
    "OCR_FORMATS",
    "PARSERS",
    "Parser",
    "RequiresOcr",
    "get_parser",
    "parse_docx",
    "parse_html",
    "parse_pdf_native",
    "parse_pptx",
    "parse_txt",
    "parse_xlsx",
]
