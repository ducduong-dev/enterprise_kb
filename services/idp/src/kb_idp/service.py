"""IDP orchestration: bytes in, KBDoc out.

One entry point (`process`) used by the Temporal activity, the HTTP endpoint and the tests, so
there is exactly one description of what "processing a document" means.

Two routes converge on the same contract:

* **digital-native** — a parser reads the text layer (M1);
* **scanned** — the OCR pipeline rasterizes, preprocesses, recognizes, and escalates the pages
  it does not trust (M3).

Which route a document takes is decided by its bytes, never by its extension. A scan with no
OCR adapter configured is still a routing outcome rather than a failure: the document is
recorded and queued, not lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kb_common.config import get_settings
from kb_common.logging import get_logger
from kb_ports.models import OcrPort, VlmPort
from kb_schemas.kbdoc import DocMeta, KBDoc, SourceFormat

from kb_idp.detect import detect_format
from kb_idp.ocr_pipeline import OcrPipeline, PageArtifact
from kb_idp.parsers import OCR_FORMATS, RequiresOcr, get_parser

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IdpResult:
    kbdoc: KBDoc
    #: True when the document needs OCR and no OCR adapter was available. `kbdoc` is empty;
    #: callers must branch on this before using it.
    requires_ocr: bool = False
    reason: str | None = None
    #: Rendered page images, present only on the scanned route. The review editor shows these
    #: beside the recognized text, so they are part of the artefact, not scratch.
    pages: list[PageArtifact] = field(default_factory=list)

    @property
    def scanned(self) -> bool:
        return bool(self.pages)


def process(
    data: bytes,
    filename: str | None = None,
    *,
    ocr: OcrPort | None = None,
    vlm: VlmPort | None = None,
) -> IdpResult:
    """Parse an uploaded document into the normalized format.

    Raises `UnsupportedFormat` for things we cannot read at all.
    """
    detected = detect_format(data, filename)
    log.info(
        "idp_detected",
        extra={
            "source": filename,
            "source_format": detected.source_format,
            "bytes": len(data),
            "mismatched_extension": detected.mismatched_extension,
        },
    )

    if detected.source_format in OCR_FORMATS:
        return _run_ocr(data, detected.source_format, filename, ocr=ocr, vlm=vlm)

    try:
        kbdoc = get_parser(detected.source_format)(data)
    except RequiresOcr as exc:
        # A PDF whose text layer turned out to be decorative. Detection could not know that
        # without parsing it, so the route is chosen here instead.
        log.info("pdf_has_no_text_layer", extra={"source": filename, "reason": exc.message})
        return _run_ocr(data, "pdf_scanned", filename, ocr=ocr, vlm=vlm)

    if detected.mismatched_extension:
        kbdoc.idp_report.warnings.append(
            f"file content does not match its extension ({detected.detail})"
        )
    _log_complete(filename, kbdoc, route="native")
    return IdpResult(kbdoc=kbdoc)


def _run_ocr(
    data: bytes,
    source_format: SourceFormat,
    filename: str | None,
    *,
    ocr: OcrPort | None,
    vlm: VlmPort | None,
) -> IdpResult:
    # `KB_MODEL_OCR_ENGINE=vlm` reads every page with the vision model instead of escalating
    # only the pages OCR could not read (ADR-0025).
    vlm_first = get_settings().models.ocr_engine == "vlm" and vlm is not None
    if ocr is None and not vlm_first:
        log.info("idp_requires_ocr", extra={"source": filename, "source_format": source_format})
        return IdpResult(
            kbdoc=KBDoc(doc_meta=DocMeta(source_format=source_format)),
            requires_ocr=True,
            reason="document has no text layer and no OCR engine is configured",
        )

    outcome = OcrPipeline(ocr=ocr, vlm=vlm, vlm_first=vlm_first).process(
        data, source_format=source_format
    )
    _log_complete(filename, outcome.kbdoc, route="ocr")
    return IdpResult(kbdoc=outcome.kbdoc, pages=outcome.pages)


def _log_complete(filename: str | None, kbdoc: KBDoc, *, route: str) -> None:
    log.info(
        "idp_complete",
        extra={
            "source": filename,
            "route": route,
            "blocks": len(kbdoc.blocks),
            "pages": kbdoc.doc_meta.page_count,
            "language": kbdoc.doc_meta.language,
            "legal_number": kbdoc.doc_meta.legal_number,
            "detected_refs": len(kbdoc.detected_refs),
            "low_confidence_blocks": len(kbdoc.low_confidence_blocks),
            "escalated_pages": kbdoc.idp_report.escalated_pages,
            "duration_ms": kbdoc.idp_report.duration_ms,
        },
    )
