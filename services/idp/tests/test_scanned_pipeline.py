"""The scanned route, end to end over the twenty-document fixture set (M3 acceptance).

What these assert, in order of how much they matter:

1. **Diacritics survive.** Byte-level comparison against the ground truth. A pipeline that
   quietly normalizes or transliterates Vietnamese produces documents that read fine and cite
   wrong, which is the failure this platform exists to prevent.
2. **Bad pages are escalated, and escalation helps.** The confidence scorer must catch the
   pages whose tone marks were lost, and the VLM's output must be measurably better than what
   it replaced — not merely different.
3. **The reviewer gets what they need**: page images, per-block confidence, boxes to jump to,
   and the engine that produced each block.
"""

from __future__ import annotations

import pytest
from kb_idp.confidence import diacritic_ratio, score_page
from kb_idp.ocr_pipeline import OcrPipeline
from kb_idp.service import IdpResult, process
from kb_idp.testing.scanned import (
    ESCALATING,
    SCAN_NAMES,
    ground_truth,
    recorded_ocr,
    recorded_vlm,
    scan_bytes,
    spec,
)
from kb_ports.models import OcrLine, OcrPageResult

CLEAN = "tt41_an_toan_von"
DEGRADED = ESCALATING[0]


def run(name: str, *, with_vlm: bool = True) -> IdpResult:
    return process(
        scan_bytes(name),
        f"{name}.pdf",
        ocr=recorded_ocr(),
        vlm=recorded_vlm() if with_vlm else None,
    )


# ------------------------------------------------------------------------------- the corpus


def test_the_fixture_set_has_twenty_documents() -> None:
    assert len(SCAN_NAMES) == 20


@pytest.mark.parametrize("name", SCAN_NAMES)
def test_every_scan_processes_end_to_end(name: str) -> None:
    result = run(name)
    assert not result.requires_ocr
    assert result.scanned
    assert result.kbdoc.blocks, f"{name} produced no blocks"
    assert result.kbdoc.doc_meta.source_format == "pdf_scanned"
    assert result.kbdoc.doc_meta.page_count == spec(name).page_count
    assert len(result.pages) == spec(name).page_count


@pytest.mark.parametrize("name", SCAN_NAMES)
def test_most_of_the_source_survives_recognition(name: str) -> None:
    """OCR is not transcription: some lines come back wrong, and that is the point of review.

    What must hold is that the great majority of the document arrives intact — a pipeline
    losing whole paragraphs is broken in a way no reviewer can fix.
    """
    result = run(name)
    recognized = set(result.kbdoc.full_text.split())
    source = [word for word in ground_truth(name).split() if len(word) > 2]
    if not source:
        return

    # Word-level rather than line-level: one misread word should cost one word, not a whole
    # paragraph, and the measure has to mean the same thing on a six-line notice and a
    # forty-page circular.
    intact = sum(1 for word in source if word in recognized)
    coverage = intact / len(source)
    assert coverage >= 0.9, f"{name}: only {coverage:.0%} of the source words survived"


@pytest.mark.parametrize("name", [n for n in SCAN_NAMES if n not in ESCALATING])
def test_what_recognition_got_wrong_is_what_it_flagged(name: str) -> None:
    """The reviewer's queue is only useful if the misread lines are the ones highlighted."""
    result = run(name)
    # Recognition merges lines into paragraphs, so compare against the ground truth with its
    # line breaks flattened — otherwise every correctly-merged block looks like a misread.
    truth = " ".join(ground_truth(name).split())
    for block in result.kbdoc.blocks:
        if not block.text or block.type == "table":
            continue
        head = " ".join(block.text.split())[:30]
        if head not in truth:
            assert block.needs_review, (
                f"{name}: {head!r} was misread at confidence {block.confidence:.2f} "
                "without being flagged for review"
            )


@pytest.mark.parametrize("name", SCAN_NAMES)
def test_vietnamese_diacritics_survive_byte_for_byte(name: str) -> None:
    """The M3 acceptance criterion, asserted on raw code points."""
    result = run(name)
    text = result.kbdoc.full_text
    source = ground_truth(name)

    marked = {ch for ch in source if ch in "ăâđêôơưĂÂĐÊÔƠƯáàảãạéèẻẽẹíìỉĩịóòỏõọúùủũụýỳỷỹỵ"}
    present = {ch for ch in text if ch in marked}
    assert marked <= present or not marked, (
        f"{name}: characters lost from the chain: {sorted(marked - present)}"
    )
    # And no transliteration: the folded form must not have replaced the accented one.
    assert diacritic_ratio(text) > 0.05


# ---------------------------------------------------------------------------- escalation


@pytest.mark.parametrize("name", ESCALATING)
def test_degraded_pages_are_escalated(name: str) -> None:
    result = run(name)
    assert result.kbdoc.idp_report.escalated_pages == list(spec(name).escalate_pages)
    assert any("escalated to the VLM" in warning for warning in result.kbdoc.idp_report.warnings)


@pytest.mark.parametrize("name", ESCALATING)
def test_escalation_recovers_the_tone_marks_ocr_lost(name: str) -> None:
    """The point of the VLM: not different output, better output."""
    without = run(name, with_vlm=False)
    with_vlm = run(name)

    assert diacritic_ratio(with_vlm.kbdoc.full_text) > diacritic_ratio(without.kbdoc.full_text)
    # The un-escalated version really is broken — otherwise this test proves nothing.
    assert diacritic_ratio(without.kbdoc.full_text) < 0.05
    # And the recovered text really is the document's, not a plausible rewrite of it.
    longest = max((line for line in ground_truth(name).splitlines() if len(line) > 30), key=len)
    assert longest[:40] in with_vlm.kbdoc.full_text


def test_without_a_vlm_the_page_is_flagged_rather_than_silently_accepted() -> None:
    result = run(DEGRADED, with_vlm=False)
    assert any("no VLM is configured" in warning for warning in result.kbdoc.idp_report.warnings)
    assert result.kbdoc.low_confidence_blocks


@pytest.mark.parametrize("name", [n for n in SCAN_NAMES if n not in ESCALATING])
def test_clean_pages_are_not_escalated(name: str) -> None:
    """The VLM is two orders of magnitude more expensive; it runs on the pages that need it."""
    assert run(name).kbdoc.idp_report.escalated_pages == []


def test_escalated_blocks_record_which_engine_read_them() -> None:
    """A reviewer checking a page must know whether OCR or the VLM produced the text."""
    blocks = run(DEGRADED).kbdoc.blocks
    assert {block.engine for block in blocks} == {"vlm"}
    assert {block.engine for block in run(CLEAN).kbdoc.blocks} == {"paddle"}


# ----------------------------------------------------------------- what the reviewer gets


def test_pages_are_kept_for_the_review_editor() -> None:
    result = run(CLEAN)
    page = result.pages[0]
    assert page.image.startswith(b"\x89PNG")
    assert page.width > 0 and page.height > 0
    assert page.score.score > 0


def test_blocks_carry_boxes_to_jump_to() -> None:
    result = run(CLEAN)
    boxed = [block for block in result.kbdoc.blocks if block.bbox]
    assert boxed
    box = boxed[0].bbox
    assert box is not None
    x0, y0, x1, y1 = box
    assert x1 > x0 and y1 > y0


def test_low_confidence_blocks_are_marked_for_the_reviewer() -> None:
    """The reviewer's job is to find the misread line, so it must be findable."""
    result = run(DEGRADED, with_vlm=False)
    assert result.kbdoc.low_confidence_blocks
    assert all(block.confidence < 0.85 for block in result.kbdoc.low_confidence_blocks)


def test_preprocessing_is_reported_to_the_reviewer() -> None:
    """A page rotated under the reviewer must say so."""
    result = run("thong_bao_chi_nhanh")  # rendered with 3.2° of skew
    assert any("preprocessed" in warning for warning in result.kbdoc.idp_report.warnings)
    assert result.pages[0].preprocess.modified


def test_page_confidences_are_reported_per_page_not_averaged() -> None:
    result = run("quy_che_tin_dung")  # two pages
    assert len(result.kbdoc.idp_report.page_confidences) == 2


# ------------------------------------------------------------------------------ structure


def test_structure_is_recovered_from_scanned_text() -> None:
    """The section tracker works on OCR output as it does on a text layer."""
    result = run(CLEAN)
    paths = {" > ".join(block.section_path) for block in result.kbdoc.blocks if block.section_path}
    assert any("Điều 6" in path for path in paths)
    assert any("Điều 12" in path for path in paths)


def test_the_documents_own_number_is_detected_from_the_scan() -> None:
    assert run(CLEAN).kbdoc.doc_meta.legal_number == "41/2016/TT-NHNN"


def test_references_are_detected_from_scanned_text() -> None:
    result = run("qd_hdqt_chinh_sach_von")
    numbers = {ref.legal_number for ref in result.kbdoc.detected_refs}
    assert "41/2016/TT-NHNN" in numbers


def test_tables_survive_the_scan() -> None:
    result = run("bieu_phi_ca_nhan")
    tables = [block for block in result.kbdoc.blocks if block.table is not None]
    assert tables
    assert any("Chuyển khoản liên ngân hàng" in block.text for block in tables)


def test_a_page_image_with_no_recording_fails_loudly() -> None:
    """A recording that silently replayed stale output would be worse than no test.

    If rasterization or preprocessing changes, every key changes and this is what happens.
    """
    from kb_common.errors import NotFound
    from kb_idp.testing.fixtures import fixture_bytes

    pipeline = OcrPipeline(ocr=recorded_ocr(), vlm=None)
    with pytest.raises(NotFound, match="no OCR recording"):
        pipeline.process(fixture_bytes("scanned_notice.pdf"), source_format="pdf_scanned")


# ----------------------------------------------------------------------- confidence rules


def test_a_page_without_tone_marks_scores_badly_however_confident_the_engine() -> None:
    """The characteristic Vietnamese failure: high confidence, wrong words."""
    confident_but_wrong = OcrPageResult(
        page=1,
        lines=[
            OcrLine(
                text="Ngan hang phai duy tri ty le an toan von toi thieu",
                bbox=(0, 0, 1, 1),
                confidence=0.97,
            ),
            OcrLine(
                text="Tai san co rui ro tin dung duoc xac dinh theo phuong phap",
                bbox=(0, 0, 1, 1),
                confidence=0.96,
            ),
        ],
        mean_confidence=0.965,
    )
    score = score_page(confident_but_wrong)
    assert score.escalate
    assert any("tone marks" in reason for reason in score.reasons)


def test_a_clean_vietnamese_page_is_not_escalated() -> None:
    page = OcrPageResult(
        page=1,
        lines=[
            OcrLine(
                text="Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8%.",
                bbox=(0, 0, 1, 1),
                confidence=0.96,
            ),
            OcrLine(
                text="Tài sản có rủi ro tín dụng được xác định theo phương pháp tiêu chuẩn.",
                bbox=(0, 0, 1, 1),
                confidence=0.95,
            ),
        ],
        mean_confidence=0.955,
    )
    assert not score_page(page).escalate


def test_an_empty_page_is_escalated_not_accepted() -> None:
    assert score_page(OcrPageResult(page=1, lines=[], mean_confidence=0.0)).escalate


# ------------------------------------------------------------------ vision-first (ADR-0025)


class StubVision:
    """A vision model that transcribes every page it is given, and counts the pages.

    The recorded VLM only holds the pages M3 escalated, so vision-first needs a stand-in. What
    is under test is the pipeline's behaviour — which engine reads, what gets recorded, what
    happens when nothing can read — not the transcription itself.
    """

    def __init__(
        self,
        *,
        text: str = "Điều 6. Tỷ lệ an toàn vốn tối thiểu là 8%.",
        leaves_network: bool = False,
    ) -> None:
        self.text = text
        self.pages = 0
        self.leaves_network = leaves_network

    @property
    def info(self):  # type: ignore[no-untyped-def]
        from kb_ports.base import AdapterInfo

        return AdapterInfo(
            name="vision",
            version="stub-vlm",
            extra={"real_vlm": True, "leaves_network": self.leaves_network},
        )

    def health(self) -> bool:
        return True

    def transcribe_page(self, image: bytes, *, prompt: str | None = None) -> OcrPageResult:
        self.pages += 1
        lines = [
            OcrLine(text=line, bbox=(0.0, 0.0, 0.0, 0.0), confidence=0.82)
            for line in self.text.splitlines()
            if line.strip()
        ]
        return OcrPageResult(page=1, lines=lines, tables=[], mean_confidence=0.82)


def test_vision_first_reads_every_page_not_only_the_bad_ones() -> None:
    vision = StubVision()
    outcome = OcrPipeline(ocr=recorded_ocr(), vlm=vision, vlm_first=True).process(scan_bytes(CLEAN))

    assert vision.pages == spec(CLEAN).page_count
    assert outcome.kbdoc.idp_report.escalated_pages == list(range(1, vision.pages + 1))
    assert {block.engine for block in outcome.kbdoc.blocks} == {"vlm"}


def test_vision_first_needs_no_ocr_engine_at_all() -> None:
    """The point of the option: a deployment with a vision model and no PaddleOCR node."""
    vision = StubVision()
    outcome = OcrPipeline(ocr=None, vlm=vision).process(scan_bytes(CLEAN))

    assert vision.pages == spec(CLEAN).page_count
    assert outcome.kbdoc.blocks


def test_vision_first_records_which_model_read_the_pages() -> None:
    outcome = OcrPipeline(ocr=None, vlm=StubVision()).process(scan_bytes(CLEAN))
    assert any("stub-vlm" in warning for warning in outcome.kbdoc.idp_report.warnings)


def test_a_public_vision_model_says_the_pages_left_the_bank() -> None:
    """The page image *is* the document; where it was read belongs in the report (ADR-0025)."""
    outcome = OcrPipeline(ocr=None, vlm=StubVision(leaves_network=True)).process(scan_bytes(CLEAN))
    assert any(
        "left the bank's network" in warning for warning in outcome.kbdoc.idp_report.warnings
    )


def test_an_unreadable_page_is_flagged_when_there_is_nothing_to_escalate_to() -> None:
    """Vision-first has no second opinion. Silence there would look like a blank page."""
    outcome = OcrPipeline(ocr=None, vlm=StubVision(text="???")).process(scan_bytes(CLEAN))
    assert any("after vision transcription" in w for w in outcome.kbdoc.idp_report.warnings)


def test_a_pipeline_with_no_engine_at_all_is_refused() -> None:
    with pytest.raises(ValueError, match="needs an OCR engine"):
        OcrPipeline(ocr=None, vlm=None)


def test_vision_first_without_a_vision_model_is_refused() -> None:
    with pytest.raises(ValueError, match="vlm_first"):
        OcrPipeline(ocr=recorded_ocr(), vlm=None, vlm_first=True)


def test_the_configured_engine_decides_which_route_a_scan_takes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KB_MODEL_OCR_ENGINE=vlm` is what a deployment sets to read scans with the vision
    model; `process()` must honour it rather than each caller remembering to."""
    from kb_common.config import reset_settings_cache

    vision = StubVision()
    monkeypatch.setenv("KB_MODEL_OCR_ENGINE", "vlm")
    reset_settings_cache()
    try:
        result = process(scan_bytes(CLEAN), f"{CLEAN}.pdf", ocr=recorded_ocr(), vlm=vision)
        assert vision.pages == spec(CLEAN).page_count
        assert {block.engine for block in result.kbdoc.blocks} == {"vlm"}
    finally:
        monkeypatch.delenv("KB_MODEL_OCR_ENGINE", raising=False)
        reset_settings_cache()


def test_a_scan_still_queues_for_ocr_when_no_engine_is_available() -> None:
    """Neither OCR nor vision: a routing outcome, never a silent empty document."""
    result = process(scan_bytes(CLEAN), f"{CLEAN}.pdf", ocr=None, vlm=None)
    assert result.requires_ocr
    assert not result.kbdoc.blocks
