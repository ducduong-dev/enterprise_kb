"""Chunker behaviour — the properties retrieval quality and citation correctness depend on."""

from __future__ import annotations

import pytest
from kb_idp.builder import KBDocBuilder
from kb_idp.service import process
from kb_idp.testing.fixtures import ALL_FIXTURES, fixture_bytes
from kb_indexer.chunker import (
    MAX_CHARS,
    Chunk,
    article_number,
    chunk_document,
    split_section_path,
    subject_key,
)
from kb_schemas.kbdoc import KBDoc
from kb_vntext.sections import build_anchor


def _doc(blocks: list[tuple[str, str]]) -> KBDoc:
    """A KBDoc from (text, block_type) pairs — for the structural questions below, where a
    real parser fixture would only add noise."""
    builder = KBDocBuilder(source_format="docx")
    for body, block_type in blocks:
        builder.add(body, block_type=block_type)  # type: ignore[arg-type]
    return builder.build(page_count=1)


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


# ---------------------------------------------- the article and subject columns (M9b)


def test_every_chunk_knows_which_article_it_belongs_to() -> None:
    """Stored rather than re-derived: the supersession predicate filters on it and the
    reference resolver joins on it (ADR-0032/0036)."""
    doc = _doc(
        [
            ("Chương II. TỶ LỆ AN TOÀN VỐN", "heading"),
            ("Điều 6. Tỷ lệ an toàn vốn", "heading"),
            ("Ngân hàng duy trì tỷ lệ an toàn vốn tối thiểu 8%.", "paragraph"),
            ("Điều 12. Tài sản có rủi ro", "heading"),
            ("Tài sản có rủi ro được xác định theo phương pháp tiêu chuẩn.", "paragraph"),
        ]
    )
    chunks = chunk_document(doc)
    assert {chunk.article for chunk in chunks} == {6, 12}


def test_a_substantial_clause_is_addressable_on_its_own() -> None:
    """`article` alone cannot tell Khoản 2 from Khoản 3, and a reference to "khoản 2 Điều 12"
    has to resolve to one clause rather than four (ADR-0036)."""
    body = "Ngân hàng thương mại phải duy trì tỷ lệ an toàn vốn tối thiểu theo quy định. " * 12
    doc = _doc(
        [
            ("Điều 12. Tỷ lệ an toàn vốn", "heading"),
            (f"1. {body}", "paragraph"),
            (f"2. {body}", "paragraph"),
        ]
    )
    assert {chunk.anchor for chunk in chunk_document(doc)} == {"12.1", "12.2"}


def test_merged_clauses_back_the_anchor_off_to_the_article() -> None:
    """Two short clauses share a chunk, and its anchor becomes the article they have in
    common — a chunk must never cite one location while containing another's text.

    Which means a reference to "khoản 2 Điều 12" can find no chunk anchored exactly there,
    even though the text is present under `"12"`. The resolver has to fall back to the article
    rather than report the anchor unresolved; the alternative is telling a steward a perfectly
    good reference is broken (ADR-0036).
    """
    doc = _doc(
        [
            ("Điều 12. Tỷ lệ an toàn vốn", "heading"),
            ("1. Ngân hàng duy trì tỷ lệ tối thiểu 8%.", "paragraph"),
            ("2. Chi nhánh nước ngoài duy trì tỷ lệ tối thiểu 9%.", "paragraph"),
        ]
    )
    chunks = chunk_document(doc)
    assert {chunk.anchor for chunk in chunks} == {"12"}
    assert all(chunk.article == 12 for chunk in chunks)


def test_a_chunk_outside_any_article_has_no_article() -> None:
    """Front matter, an appendix, a table before Điều 1. NULL means the chunk is flagged only
    when the whole document is — an amendment to Điều 12 does not date the appendix."""
    doc = _doc(
        [
            ("Phụ lục I. Biểu mẫu báo cáo", "heading"),
            ("Đơn vị lập báo cáo điền đầy đủ các chỉ tiêu dưới đây.", "paragraph"),
        ]
    )
    assert all(chunk.article is None for chunk in chunk_document(doc))


def test_the_chunker_and_the_diff_recover_the_same_article() -> None:
    """Two modules read the article out of a section path — the chunker for the column, the
    diff for the impact traversal. They must not disagree."""
    from kb_identity_merge.diff import _article_number

    for path in (["Điều 6"], ["Chương II", "Điều 12", "Khoản 2"], ["Phụ lục I"], []):
        assert article_number(path) == _article_number(" > ".join(path))


def test_the_subject_key_survives_a_rewording() -> None:
    """The whole point: two clauses stating the same rule, phrased differently by different
    departments years apart, have to land on the same key (ADR-0033)."""
    one = subject_key(["Mục 2. Lãi suất cho vay đối với khách hàng cá nhân"])
    two = subject_key(["Điều 8. Lãi suất cho vay khách hàng cá nhân"])
    assert one and one == two


def test_a_purely_structural_path_has_no_subject() -> None:
    """ "Điều 6" says what the clause *is*, not what it is about, and a key that says nothing
    would match everything."""
    assert subject_key(["Chương I", "Điều 1"]) is None


def test_two_documents_structured_differently_share_one_subject_key() -> None:
    """The only thing this key is for, and what unioning the whole chain defeated.

    A regulator files the rule under a chapter about capital; a bank files its restatement
    under a part about capital *management*. Same rule, same article title, and under the old
    whole-chain union the ancestor "Quản lý vốn" put them in different fact sets — so the
    channel fired only between documents of the same structural shape, which is the case that
    least needs it (ADR-0037).
    """
    regulator = subject_key(
        ["Chương II. Tỷ lệ an toàn vốn", "Điều 6. Tỷ lệ an toàn vốn tối thiểu", "Khoản 1"]
    )
    bank = subject_key(["Phần 2. Quản lý vốn", "Mục 3. Tỷ lệ an toàn vốn tối thiểu"])

    assert regulator is not None
    assert regulator == bank


def test_an_untitled_leaf_falls_through_to_the_heading_that_has_a_title() -> None:
    """ "Khoản 1" says what the clause is and never what it is about, so the article above it
    is the deepest thing that answers the question."""
    assert subject_key(["Điều 6. Tỷ lệ an toàn vốn", "Khoản 1"]) == subject_key(
        ["Điều 6. Tỷ lệ an toàn vốn"]
    )


def test_a_boilerplate_heading_has_no_subject() -> None:
    """Every instrument carries one. Keying on it linked the closing article of each document
    to the closing article of every other — 28 chunks across two documents in this corpus."""
    for title in (
        "Điều 8. Hiệu lực thi hành",
        "Điều 2. Đối tượng áp dụng",
        "Điều 3. Giải thích từ ngữ",
    ):
        assert subject_key([title]) is None, title


def test_boilerplate_does_not_fall_back_to_its_parent() -> None:
    """Worse than nothing: it would file a capital circular's effectivity article under capital
    adequacy, which is a confident wrong answer rather than an absent one."""
    assert subject_key(["Chương II. Tỷ lệ an toàn vốn", "Điều 15. Hiệu lực thi hành"]) is None


def test_boilerplate_words_still_count_inside_a_real_subject() -> None:
    """ "hiệu" is in "hiệu quả" and "thi" in "thi công". These are boilerplate as whole
    headings, which is why they are matched there and not stopworded token by token."""
    assert subject_key(["Điều 9. Hiệu quả sử dụng vốn"]) is not None
    assert subject_key(["Điều 4. Giám sát thi công công trình"]) is not None


def test_the_subject_key_is_order_independent_and_folded() -> None:
    assert subject_key(["Tỷ lệ an toàn vốn"]) == subject_key(["Vốn an toàn tỷ lệ"])
    assert subject_key(["Dự trữ bắt buộc"]) == subject_key(["Du tru bat buoc"])


def test_the_backfill_derives_what_the_chunker_would_have_written() -> None:
    """`scripts/backfill_chunk_article.py` fills `article` and `anchor` from the stored
    `section_path` instead of rechunking. That is only safe if it produces the same answer, so
    the claim is asserted rather than trusted.

    `subject_key` is deliberately *not* in that list, and the second half of this test is why:
    it cannot be recovered from `section_path`, because a path carries structural labels and a
    subject key needs the heading title. The backfill used to derive it anyway and wrote NULL
    for every row in the corpus. Asserting the absence keeps anyone from adding it back."""
    doc = _doc(
        [
            ("Chương II. TỶ LỆ AN TOÀN VỐN", "heading"),
            ("Điều 6. Tỷ lệ an toàn vốn", "heading"),
            ("Ngân hàng duy trì tỷ lệ an toàn vốn tối thiểu 8%.", "paragraph"),
            ("Phụ lục I. Biểu mẫu", "heading"),
            ("Đơn vị lập báo cáo điền đầy đủ chỉ tiêu.", "paragraph"),
        ]
    )
    for chunk in chunk_document(doc):
        # What the backfill sees in the database is the text form, not the list.
        recovered = split_section_path(chunk.section_path_text)
        assert article_number(recovered) == chunk.article
        assert build_anchor(recovered) == chunk.anchor

    # And the one that needs a rechunk: the titles the key is built from were never in the path.
    titled = [chunk for chunk in chunk_document(doc) if chunk.subject_key]
    assert titled, "the fixture has titled headings, so some chunk must carry a subject key"
    for chunk in titled:
        assert subject_key(split_section_path(chunk.section_path_text)) is None
