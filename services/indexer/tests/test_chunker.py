"""Chunker behaviour — the properties retrieval quality and citation correctness depend on."""

from __future__ import annotations

import pytest
from kb_idp.service import process
from kb_idp.testing.fixtures import ALL_FIXTURES, fixture_bytes
from kb_indexer.chunker import MAX_CHARS, Chunk, chunk_document
from kb_schemas.kbdoc import KBDoc


def chunks_for(name: str) -> list[Chunk]:
    result = process(fixture_bytes(name), name)
    assert not result.requires_ocr
    return chunk_document(result.kbdoc)


def test_chunks_carry_real_citations() -> None:
    """The label is what a reviewer quotes, so it names a location, not a chunk number."""
    labels = {chunk.citation_label for chunk in chunks_for("tt41_capital.docx")}
    assert "Điều 12, 41/2016/TT-NHNN" in labels
    assert "Điều 6, 41/2016/TT-NHNN" in labels


def test_a_substantial_clause_is_cited_at_clause_level() -> None:
    """Short clauses merge up to their article; a clause big enough to stand alone keeps its
    own citation, which is the granularity a compliance officer works at."""
    body = "Ngân hàng phải xác định hệ số rủi ro theo phương pháp tiêu chuẩn. " * 10
    kbdoc = _synthetic(
        [
            ("Điều 12. Tài sản có rủi ro tín dụng", "heading"),
            (f"1. {body}", "paragraph"),
            (f"2. {body}", "paragraph"),
        ]
    )
    labels = {c.citation_label for c in chunk_document(kbdoc, legal_number="41/2016/TT-NHNN")}
    assert "Điều 12.1, 41/2016/TT-NHNN" in labels
    assert "Điều 12.2, 41/2016/TT-NHNN" in labels


def test_every_chunk_carries_its_ancestors_in_the_text() -> None:
    """A clause saying "hệ số rủi ro" must still match a query naming its article."""
    chunks = chunks_for("tt41_capital.docx")
    clause = next(c for c in chunks if "Hệ số rủi ro" in c.text)
    assert "Điều 12" in clause.text
    assert "Chương II" in clause.text


def test_a_chunk_never_mixes_two_articles() -> None:
    """A chunk citing one article while containing another's text is unverifiable."""
    for name in ("tt41_capital.docx", "amendment.docx", "circular_native.pdf"):
        for chunk in chunks_for(name):
            articles = {part for part in chunk.section_path if part.startswith(("Điều", "Article"))}
            assert len(articles) <= 1


def test_tables_are_never_split_or_merged() -> None:
    chunks = chunks_for("fees.xlsx")
    tables = [chunk for chunk in chunks if chunk.is_table]
    assert tables
    for table in tables:
        assert table.part_count == 1
        # The whole grid, header row included.
        assert "Dịch vụ" in table.text
        assert "Chuyển khoản liên ngân hàng" in table.text


def test_short_neighbouring_clauses_are_merged() -> None:
    """A three-word clause retrieves nothing on its own."""
    kbdoc = _synthetic(
        [
            ("Điều 5. Hiệu lực", "heading"),
            ("1. Có hiệu lực từ 01/01/2026.", "paragraph"),
            ("2. Áp dụng toàn hệ thống.", "paragraph"),
        ]
    )
    chunks = chunk_document(kbdoc, legal_number="99/2026/TT-TEST")
    assert len(chunks) == 1
    assert "01/01/2026" in chunks[0].text
    assert "toàn hệ thống" in chunks[0].text
    # Merged across clauses, so the citation backs off to the article.
    assert chunks[0].citation_label == "Điều 5, 99/2026/TT-TEST"


def test_a_long_clause_is_split_but_keeps_one_citation() -> None:
    body = "Nội dung quy định chi tiết. " * 200
    kbdoc = _synthetic([("Điều 9. Quy định chi tiết", "heading"), (f"1. {body}", "paragraph")])
    chunks = chunk_document(kbdoc, legal_number="99/2026/TT-TEST")

    assert len(chunks) > 1
    assert all(chunk.part_count == len(chunks) for chunk in chunks)
    assert {chunk.citation_label for chunk in chunks} == {"Điều 9.1, 99/2026/TT-TEST"}
    assert all(len(chunk.text) <= MAX_CHARS + 200 for chunk in chunks)


def test_split_parts_overlap_so_a_straddling_sentence_stays_findable() -> None:
    body = " ".join(f"Câu số {i} về tỷ lệ an toàn vốn." for i in range(200))
    kbdoc = _synthetic([("Điều 9. Chi tiết", "heading"), (f"1. {body}", "paragraph")])
    chunks = chunk_document(kbdoc)
    assert len(chunks) > 1
    tail = chunks[0].text[-80:]
    assert any(fragment and fragment in chunks[1].text for fragment in [tail[-40:]])


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_chunks_into_usable_units(name: str) -> None:
    chunks = chunks_for(name)
    assert chunks, f"{name} produced no chunks"
    assert all(chunk.text.strip() for chunk in chunks)
    assert all(chunk.ordinal == index for index, chunk in enumerate(chunks))
    assert all(len(chunk.text) <= MAX_CHARS + 400 for chunk in chunks)
    # No chunk is only its own heading line: such a chunk matches queries it cannot answer.
    for chunk in chunks:
        body = chunk.text.replace(chunk.section_path_text, "", 1).strip()
        assert body, f"{name}: heading-only chunk {chunk.citation_label!r}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_chunking_loses_no_content(name: str) -> None:
    """Every block's text must survive into some chunk — a dropped clause is invisible."""
    result = process(fixture_bytes(name), name)
    chunks = chunk_document(result.kbdoc)
    combined = "\n".join(chunk.text for chunk in chunks)
    for block in result.kbdoc.blocks:
        first_line = block.text.splitlines()[0] if block.text else ""
        if len(first_line) > 20:
            assert first_line[:60] in combined, f"{name}: lost {first_line[:60]!r}"


def test_chunk_ids_are_unique() -> None:
    chunks = chunks_for("tt41_capital.docx")
    assert len({chunk.id for chunk in chunks}) == len(chunks)


def _synthetic(lines: list[tuple[str, str]]) -> KBDoc:
    """Build a KBDoc through the real builder so structure tracking is exercised."""
    from kb_idp.builder import KBDocBuilder

    builder = KBDocBuilder(source_format="docx")
    for text, block_type in lines:
        builder.add(text, block_type=block_type)  # type: ignore[arg-type]
    return builder.build(page_count=1)
