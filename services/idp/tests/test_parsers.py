"""Parser behaviour the goldens summarize but do not explain."""

from __future__ import annotations

import pytest
from kb_idp.detect import UnsupportedFormat, detect_format
from kb_idp.parsers import RequiresOcr, get_parser
from kb_idp.service import process
from kb_idp.testing.fixtures import OCR_ROUTING_FIXTURE, fixture_bytes
from kb_schemas.kbdoc import KBDoc


def parse(name: str) -> KBDoc:
    result = process(fixture_bytes(name), name)
    assert not result.requires_ocr
    return result.kbdoc


# ------------------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tt41_capital.docx", "docx"),
        ("fees.xlsx", "xlsx"),
        ("training.pptx", "pptx"),
        ("circular_native.pdf", "pdf_native"),
        ("faq.html", "html"),
        ("notice.txt", "txt"),
    ],
)
def test_format_is_detected_from_content(name: str, expected: str) -> None:
    assert detect_format(fixture_bytes(name), name).source_format == expected


def test_a_mislabelled_upload_is_routed_by_content_and_flagged() -> None:
    """A .docx renamed to .pdf must parse as DOCX, and the reviewer must be told."""
    kbdoc = parse_named("tt41_capital.docx", filename="tt41_capital.pdf")
    assert kbdoc.doc_meta.source_format == "docx"
    assert any("does not match its extension" in w for w in kbdoc.idp_report.warnings)


def parse_named(fixture: str, *, filename: str) -> KBDoc:
    result = process(fixture_bytes(fixture), filename)
    assert not result.requires_ocr
    return result.kbdoc


def test_unreadable_formats_are_refused_not_guessed() -> None:
    with pytest.raises(UnsupportedFormat):
        process(b"\xd0\xcf\x11\xe0legacy .doc content", "old.doc")
    with pytest.raises(UnsupportedFormat):
        process(b"\x00\x01\x02\x03\xff\xfe", "mystery.bin")


def test_ocr_formats_have_no_parser() -> None:
    with pytest.raises(RequiresOcr):
        get_parser("image")


def test_scanned_pdf_is_a_routing_outcome_not_an_error() -> None:
    result = process(fixture_bytes(OCR_ROUTING_FIXTURE), OCR_ROUTING_FIXTURE)
    assert result.requires_ocr
    assert result.kbdoc.doc_meta.source_format == "pdf_scanned"


# --------------------------------------------------------------------------------- content


def test_vietnamese_text_survives_byte_for_byte() -> None:
    """No NFC/NFD folding, no transliteration, anywhere in the chain."""
    kbdoc = parse("tt41_capital.docx")
    text = kbdoc.full_text
    for phrase in (
        "tỷ lệ an toàn vốn",
        "Tài sản có rủi ro tín dụng",
        "Điều 12",
        "Khoản 3 Điều này",
    ):
        assert phrase in text
    # Precomposed form preserved: "ỷ" must not have become "y" + combining hook.
    assert "ỷ" in text or "tỷ" in text
    assert "ỷ" not in text


def test_structure_is_attached_to_every_block() -> None:
    kbdoc = parse("tt41_capital.docx")
    clause = next(b for b in kbdoc.blocks if b.text.startswith("2. Hệ số rủi ro"))
    assert clause.section_path == ["Chương II", "Điều 12", "Khoản 2"]


def test_tables_keep_their_grid_and_gain_searchable_text() -> None:
    kbdoc = parse("tt41_capital.docx")
    table_block = next(b for b in kbdoc.blocks if b.table is not None)
    assert table_block.table is not None
    assert table_block.table.rows[0] == ["Loại tài sản", "Hệ số rủi ro", "Ghi chú"]
    assert "Hệ số rủi ro" in table_block.text  # retrievable, not just structured


def test_workbook_sheets_become_tables_and_hidden_sheets_are_skipped() -> None:
    kbdoc = parse("risk_limits.xlsx")
    assert len([b for b in kbdoc.blocks if b.table is not None]) == 2
    assert any("hidden sheet" in w for w in kbdoc.idp_report.warnings)


def test_slide_notes_are_captured() -> None:
    kbdoc = parse("training.pptx")
    assert "QT-2024-007" in kbdoc.full_text


def test_html_scripts_and_styles_are_not_content() -> None:
    kbdoc = parse("faq.html")
    assert "console.log" not in kbdoc.full_text
    assert "font-family" not in kbdoc.full_text
    assert "Miễn phí" in kbdoc.full_text


def test_pdf_blocks_carry_page_and_bbox_for_the_review_editor() -> None:
    kbdoc = parse("circular_native.pdf")
    assert {b.page for b in kbdoc.blocks} == {1, 2}
    assert all(b.bbox is not None for b in kbdoc.blocks)
    x0, y0, x1, y1 = next(b.bbox for b in kbdoc.blocks if b.bbox)
    assert x1 > x0 and y1 > y0


def test_digital_native_blocks_are_full_confidence() -> None:
    """Confidence below 1.0 means an OCR engine guessed; a text layer did not."""
    kbdoc = parse("circular_native.pdf")
    assert all(block.confidence == 1.0 for block in kbdoc.blocks)
    assert kbdoc.low_confidence_blocks == []


def test_amendment_relationships_are_proposed_for_human_confirmation() -> None:
    kbdoc = parse("amendment.docx")
    guesses = {ref.legal_number: ref.ref_type_guess for ref in kbdoc.detected_refs}
    assert guesses["41/2016/TT-NHNN"] == "amends"
    assert guesses["1627/2001/QĐ-NHNN"] == "abrogates"
    assert all(ref.block_id for ref in kbdoc.detected_refs)  # traceable to a location


def test_a_cited_number_is_never_taken_as_the_document_number() -> None:
    """`documents.legal_number` is unique — a false positive collides with the real one."""
    assert parse("notice.txt").doc_meta.legal_number is None
    assert parse("risk_limits.xlsx").doc_meta.legal_number is None
    assert parse("tt41_capital.docx").doc_meta.legal_number == "41/2016/TT-NHNN"


def test_bilingual_documents_are_detected_as_mixed() -> None:
    assert parse("policy_bilingual.docx").doc_meta.language == "mixed"
    assert parse("english_policy.docx").doc_meta.language == "en"
    assert parse("tt41_capital.docx").doc_meta.language == "vi"


def test_report_records_which_engine_produced_the_output() -> None:
    """Provenance: a reviewer must be able to tell native extraction from OCR."""
    assert parse("tt41_capital.docx").idp_report.engine_versions == {"python-docx": "native"}
    assert "pymupdf" in parse("circular_native.pdf").idp_report.engine_versions


def test_the_parser_reads_when_the_instrument_takes_effect() -> None:
    """`effective_from` decides what a point-in-time query returns and how an amendment chain
    is ordered. Leaving it NULL is not neutral — it means "always in force" (ADR-0029)."""
    from kb_idp.builder import KBDocBuilder

    builder = KBDocBuilder(source_format="txt")
    builder.add("CHÍNH PHỦ", block_type="heading")
    builder.add("Hà Nội, ngày 15 tháng 3 năm 2026")
    builder.add("Điều 4. Hiệu lực thi hành", block_type="heading")
    builder.add("Nghị định này có hiệu lực thi hành kể từ ngày 01 tháng 5 năm 2026.")
    doc = builder.build(page_count=1)

    assert doc.doc_meta.effective_from == "2026-05-01"
    assert doc.doc_meta.issued_date == "2026-03-15"
    assert "hiệu lực thi hành" in (doc.doc_meta.effective_evidence or "")


def test_a_document_that_does_not_state_its_effectivity_says_nothing() -> None:
    from kb_idp.builder import KBDocBuilder

    builder = KBDocBuilder(source_format="txt")
    builder.add("Điều 1. Phạm vi điều chỉnh", block_type="heading")
    builder.add("Quy trình này áp dụng cho toàn bộ đơn vị kinh doanh.")
    doc = builder.build(page_count=1)

    assert doc.doc_meta.effective_from is None
    assert doc.doc_meta.effective_evidence is None
