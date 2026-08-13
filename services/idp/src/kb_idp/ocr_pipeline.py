"""The scanned document route: image bytes in, `KBDoc` out.

    rasterize → preprocess → OCR → score → escalate the bad pages to the VLM → assemble

Two ways to read a page, chosen by `KB_MODEL_OCR_ENGINE` (ADR-0025):

* **paddle** (default) — PaddleOCR reads every page and the confidence scorer sends the ones it
  could not read to the vision model. Cheap, local, and the VLM is the exception.
* **vlm** — the vision model reads every page. Better on stamps, handwriting and degraded
  photocopies, and one or two orders of magnitude more expensive per page; with a *public*
  vision model it also means every page image leaves the bank, which is why that route is
  refused until Compliance switches it on ([OPEN]-1).

Either way the output is scored the same, every block records which engine produced it, and a
page nothing could read is a warning rather than an empty page.

The output is the *same* KBDoc a DOCX produces. That is the whole point of the contract: the
chunker, the registry, retrieval and the chatbots cannot tell a scanned circular from a native
one, and nothing downstream needs a second code path.

What is different is what a scan carries with it. Every block records the engine that produced
it, the confidence it was produced with, its page, and its box on that page — because a scanned
document is *provisional* until a human has looked at it, and the review editor needs all four
to make that review possible rather than notional.

Page images are kept: the reviewer compares text against the image, so the image is part of the
artefact, not a temporary.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pymupdf
from kb_common.logging import get_logger
from kb_ports.models import OcrLine, OcrPageResult, OcrPort, VlmPort
from kb_schemas.kbdoc import Engine, KBDoc, SourceFormat

from kb_idp.builder import KBDocBuilder
from kb_idp.confidence import PageScore, block_confidence, score_page
from kb_idp.preprocess import PreprocessReport, encode_png, prepare

log = get_logger(__name__)

#: Rasterization resolution. 300 DPI is the floor for reliable Vietnamese diacritics; higher
#: costs OCR time for no measured gain on this corpus.
RENDER_DPI = 300
#: Lines closer than this fraction of page height are treated as one paragraph.
LINE_GAP_RATIO = 0.012
#: A page whose text is this much wider than the median line is probably a table row grid.
_TABLE_PIPE_MIN = 2


@dataclass
class PageArtifact:
    """One rendered page, kept for the review editor.

    `width`/`height` describe the *preprocessed* image — the one served to the reviewer and
    the one the blocks' boxes are in. Reporting the pre-preprocessing size would put every
    outline in the wrong place on a page that was trimmed or upscaled.
    """

    page: int
    image: bytes
    width: int
    height: int
    preprocess: PreprocessReport
    score: PageScore
    escalated: bool = False


@dataclass
class OcrOutcome:
    kbdoc: KBDoc
    pages: list[PageArtifact] = field(default_factory=list)

    @property
    def escalated_pages(self) -> list[int]:
        return [page.page for page in self.pages if page.escalated]


class OcrPipeline:
    def __init__(
        self,
        *,
        ocr: OcrPort | None = None,
        vlm: VlmPort | None = None,
        render_dpi: int = RENDER_DPI,
        expect_vietnamese: bool = True,
        vlm_first: bool = False,
    ) -> None:
        if ocr is None and vlm is None:
            # Not a defensive check: reaching here with neither engine would produce a KBDoc
            # of empty pages that looks exactly like a successfully-read blank document.
            raise ValueError("the OCR pipeline needs an OCR engine, a vision model, or both")
        if vlm_first and vlm is None:
            raise ValueError("vlm_first was requested without a vision model")
        self._ocr = ocr
        self._vlm = vlm
        self._dpi = render_dpi
        self._expect_vietnamese = expect_vietnamese
        #: Read every page with the vision model instead of escalating only the bad ones.
        self._vlm_first = vlm_first or ocr is None

    def process(self, data: bytes, *, source_format: SourceFormat = "pdf_scanned") -> OcrOutcome:
        primary: Engine = "vlm" if self._vlm_first else "paddle"
        builder = KBDocBuilder(source_format=source_format, engine=primary)
        if self._ocr is not None:
            builder.note_engine(self._ocr.info.name, self._ocr.info.version)
        if self._vlm is not None:
            builder.note_engine(self._vlm.info.name, self._vlm.info.version)
        if self._vlm_first and self._vlm is not None:
            # The provenance a reviewer and an auditor both need: which model read the page,
            # and whether the page image left the bank to be read (ADR-0025).
            builder.warn(
                f"every page transcribed by the vision model {self._vlm.info.version}"
                + (
                    " — page images left the bank's network"
                    if self._vlm.info.extra.get("leaves_network")
                    else ""
                )
            )

        artifacts: list[PageArtifact] = []
        for page_number, raw_image, _rendered_size in self._render(data, source_format):
            image, report = prepare(raw_image, dpi=self._dpi)
            page_png = encode_png(image)

            if self._vlm_first and self._vlm is not None:
                # No OCR pass at all: the vision model is the reader, not the second opinion.
                result = self._vlm.transcribe_page(page_png)
                result.page = page_number
                score = score_page(result, expect_vietnamese=self._expect_vietnamese)
                if score.escalate:
                    # Nothing left to escalate *to* — say so rather than silently accepting a
                    # page the scorer thinks is unreadable.
                    builder.warn(
                        f"page {page_number} scored {score.score:.2f} after vision "
                        f"transcription: {'; '.join(score.reasons)}"
                    )
                self._emit_page(builder, result, page_number, escalated=True)
                artifacts.append(
                    PageArtifact(
                        page=page_number,
                        image=page_png,
                        width=int(image.shape[1]),
                        height=int(image.shape[0]),
                        preprocess=report,
                        score=score,
                        escalated=True,
                    )
                )
                continue

            assert self._ocr is not None  # guaranteed by __init__
            result = self._ocr.recognize_page(page_png, page=page_number)
            score = score_page(result, expect_vietnamese=self._expect_vietnamese)
            escalated = False

            if score.escalate and self._vlm is not None:
                # The VLM replaces the page rather than merging with it: two transcriptions of
                # the same page interleaved is worse than either alone, and the reviewer needs
                # to know which engine produced what they are checking.
                log.info(
                    "page_escalated_to_vlm",
                    extra={
                        "page": page_number,
                        "score": round(score.score, 3),
                        "reasons": score.reasons,
                    },
                )
                result = self._vlm.transcribe_page(page_png)
                result.page = page_number
                escalated = True
                builder.warn(f"page {page_number} escalated to the VLM: {'; '.join(score.reasons)}")
            elif score.escalate:
                builder.warn(
                    f"page {page_number} scored {score.score:.2f} and no VLM is configured: "
                    f"{'; '.join(score.reasons)}"
                )

            if report.modified:
                builder.warn(
                    f"page {page_number} preprocessed: {', '.join(report.steps)}"
                    + (f" (deskew {report.deskew_degrees:.1f}°)" if report.deskew_degrees else "")
                )

            self._emit_page(builder, result, page_number, escalated=escalated)
            artifacts.append(
                PageArtifact(
                    page=page_number,
                    image=page_png,
                    width=int(image.shape[1]),
                    height=int(image.shape[0]),
                    preprocess=report,
                    score=score,
                    escalated=escalated,
                )
            )

        kbdoc = builder.build(page_count=len(artifacts), source_format=source_format)
        # The report is what the reviewer's queue entry is built from, so it carries the
        # per-page scores and which pages were escalated, not just an average.
        kbdoc.idp_report.page_confidences = [round(a.score.score, 3) for a in artifacts]
        kbdoc.idp_report.escalated_pages = [a.page for a in artifacts if a.escalated]
        log.info(
            "ocr_pipeline_complete",
            extra={
                "pages": len(artifacts),
                "escalated": len(kbdoc.idp_report.escalated_pages),
                "blocks": len(kbdoc.blocks),
                "mean_page_score": round(sum(a.score.score for a in artifacts) / len(artifacts), 3)
                if artifacts
                else 0.0,
            },
        )
        return OcrOutcome(kbdoc=kbdoc, pages=artifacts)

    # ------------------------------------------------------------------------ rendering

    def _render(
        self, data: bytes, source_format: SourceFormat
    ) -> Iterator[tuple[int, Any, tuple[int, int]]]:
        if source_format == "image":
            from kb_idp.preprocess import decode_image

            image = decode_image(data)
            height, width = image.shape[:2]
            yield 1, image, (width, height)
            return

        document = pymupdf.open(stream=data, filetype="pdf")
        try:
            for index in range(document.page_count):
                pixmap = document[index].get_pixmap(dpi=self._dpi)
                yield (
                    index + 1,
                    _pixmap_to_array(pixmap),
                    (pixmap.width, pixmap.height),
                )
        finally:
            document.close()

    # ------------------------------------------------------------------------ assembly

    def _emit_page(
        self, builder: KBDocBuilder, result: OcrPageResult, page: int, *, escalated: bool
    ) -> None:
        """Group recognized lines into blocks and push them through the shared builder."""
        engine: Engine = "vlm" if escalated else "paddle"

        for rows in result.tables:
            builder.add_table(rows, header_rows=1, page=page, confidence=0.9, engine=engine)

        for group in _group_lines(result):
            text = " ".join(line.text for line in group)
            if not text.strip():
                continue
            builder.add(
                text,
                page=page,
                bbox=_merge_bbox(group) if not escalated else None,
                confidence=block_confidence([line.confidence for line in group]),
                engine=engine,
            )


def _group_lines(result: OcrPageResult) -> list[list[OcrLine]]:
    """Merge consecutive lines into paragraphs by vertical gap.

    OCR returns lines; a clause is a paragraph. Grouping on the gap between baselines is crude
    but stable, and the section tracker in the builder re-establishes the real structure from
    "Điều 12"-style openers regardless.
    """
    lines = [line for line in result.lines if line.text.strip()]
    if not lines:
        return []

    page_height = max(line.bbox[3] for line in lines) or 1.0
    threshold = page_height * LINE_GAP_RATIO * 3

    groups: list[list[OcrLine]] = [[lines[0]]]
    for previous, current in itertools.pairwise(lines):
        gap = current.bbox[1] - previous.bbox[3]
        starts_structure = _looks_structural(current.text)
        # A line that opens a structural unit always starts a new block, however tight the
        # gap: merging "Điều 13" onto the tail of Điều 12 would put two articles in one chunk.
        if gap > threshold or starts_structure:
            groups.append([current])
        else:
            groups[-1].append(current)
    return groups


def _looks_structural(text: str) -> bool:
    from kb_vntext.sections import parse_heading

    return parse_heading(text, inside_article=True) is not None


def _merge_bbox(group: list[OcrLine]) -> tuple[float, float, float, float]:
    boxes = [line.bbox for line in group]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _pixmap_to_array(pixmap: Any) -> Any:
    import numpy as np

    height, width = pixmap.height, pixmap.width
    channels = pixmap.n
    array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(height, width, channels)
    if channels == 4:
        array = array[:, :, :3]
    # PyMuPDF gives RGB; OpenCV expects BGR, and the difference matters to nothing here except
    # consistency with images decoded from files.
    return array[:, :, ::-1].copy()
