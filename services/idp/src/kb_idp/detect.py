"""Format detection.

Filenames lie — uploads arrive as `scan.pdf` that is really a JPEG, or `.doc` that is really
a `.docx`. Routing on content first and the filename only as a tiebreaker keeps a mislabelled
upload from silently taking the wrong parser and producing plausible-looking garbage.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO

from kb_common.errors import ValidationError
from kb_schemas.kbdoc import SourceFormat

_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # legacy .doc/.xls/.ppt
_IMAGE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)

_EXTENSION_HINTS: dict[str, SourceFormat] = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".pdf": "pdf_native",
    ".html": "html",
    ".htm": "html",
    ".txt": "txt",
    ".md": "txt",
}


@dataclass(frozen=True, slots=True)
class DetectedFormat:
    source_format: SourceFormat
    #: True when the bytes disagreed with the filename — recorded as an IDP warning so the
    #: reviewer sees it rather than wondering why the output looks odd.
    mismatched_extension: bool = False
    detail: str = ""


class UnsupportedFormat(ValidationError):
    code = "unsupported_format"


def detect_format(data: bytes, filename: str | None = None) -> DetectedFormat:
    suffix = _suffix(filename)
    hinted = _EXTENSION_HINTS.get(suffix)
    actual = _sniff(data)
    return DetectedFormat(
        source_format=actual,
        mismatched_extension=hinted is not None
        and hinted != actual
        and not _compatible(hinted, actual),
        detail=f"extension={suffix or 'none'} content={actual}",
    )


def _compatible(hinted: SourceFormat, actual: SourceFormat) -> bool:
    # A .pdf whose text layer is missing is still a PDF; the OCR route decides that later.
    return {hinted, actual} == {"pdf_native", "pdf_scanned"}


def _suffix(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[1].lower()


def _sniff(data: bytes) -> SourceFormat:
    if data.startswith(_PDF_MAGIC):
        return "pdf_native"  # the parser downgrades to pdf_scanned if there is no text layer
    if data.startswith(_ZIP_MAGIC):
        return _sniff_ooxml(data)
    for magic, _kind in _IMAGE_MAGICS:
        if data.startswith(magic):
            return "image"
    if data.startswith(_OLE_MAGIC):
        raise UnsupportedFormat(
            "legacy binary Office formats are not supported; save as .docx/.xlsx/.pptx",
            hint="ole2",
        )
    text = _as_text(data)
    if text is None:
        raise UnsupportedFormat("unrecognized file format")
    lowered = text[:2048].lower()
    if "<html" in lowered or "<!doctype html" in lowered:
        return "html"
    return "txt"


def _sniff_ooxml(data: bytes) -> SourceFormat:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat("file looks like a zip archive but cannot be read") from exc

    if any(name.startswith("word/") for name in names):
        return "docx"
    if any(name.startswith("xl/") for name in names):
        return "xlsx"
    if any(name.startswith("ppt/") for name in names):
        return "pptx"
    raise UnsupportedFormat("zip archive is not an Office document", entries=sorted(names)[:5])


def _as_text(data: bytes) -> str | None:
    """Decode as text, or None.

    Being strict matters: cp1258 maps nearly every byte, and a UTF-16 BOM check is the only
    thing separating a real export from arbitrary binary. Without the printability test, a
    corrupt upload decodes "successfully" into control characters and gets ingested as a
    document made of noise.
    """
    encodings = ["utf-8"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.append("cp1258")

    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _is_printable(text):
            return text
    return None


def _is_printable(text: str, threshold: float = 0.99) -> bool:
    if not text:
        return True
    allowed = sum(1 for ch in text if ch in "\n\r\t" or ch.isprintable())
    return allowed / len(text) >= threshold


def decode_text(data: bytes) -> str:
    """Decode a text-ish upload. cp1258 is the Windows Vietnamese code page — still common
    in files exported from older internal systems."""
    text = _as_text(data)
    if text is None:
        raise UnsupportedFormat("file is not decodable text")
    return text
