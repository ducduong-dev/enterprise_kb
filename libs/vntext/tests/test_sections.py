"""Structure detection and the section paths that become citation labels."""

from __future__ import annotations

import pytest
from kb_vntext.language import detect_language
from kb_vntext.sections import (
    Level,
    SectionTracker,
    build_anchor,
    build_citation_label,
    parse_heading,
)


@pytest.mark.parametrize(
    ("line", "level", "label"),
    [
        ("PHẦN THỨ NHẤT", Level.PART, "Phần THỨ NHẤT"),
        ("Chương II", Level.CHAPTER, "Chương II"),
        ("CHƯƠNG 3 QUY ĐỊNH CHUNG", Level.CHAPTER, "Chương 3"),
        ("Mục 1. Nguyên tắc chung", Level.SECTION, "Mục 1"),
        ("Điều 12. Tài sản có rủi ro", Level.ARTICLE, "Điều 12"),
        ("ĐIỀU 5", Level.ARTICLE, "Điều 5"),
        ("Điều 12a. Bổ sung", Level.ARTICLE, "Điều 12a"),
        ("Chapter II", Level.CHAPTER, "Chapter II"),
        ("Article 5 — Scope", Level.ARTICLE, "Article 5"),
        ("Section 2.1 Definitions", Level.SECTION, "Section 2.1"),
    ],
)
def test_headings_are_recognized(line: str, level: Level, label: str) -> None:
    heading = parse_heading(line)
    assert heading is not None
    assert heading.level is level
    assert heading.label == label


def test_heading_title_is_captured_separately() -> None:
    heading = parse_heading("Điều 12. Tài sản có rủi ro tín dụng")
    assert heading is not None
    assert heading.title == "Tài sản có rủi ro tín dụng"


def test_prose_is_not_a_heading() -> None:
    assert parse_heading("Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu 8%.") is None
    # A paragraph that merely begins with a heading word is prose, not structure.
    long_line = "Điều 12 của Thông tư này quy định " + "chi tiết " * 60
    assert parse_heading(long_line) is None


def test_clauses_only_count_inside_an_article() -> None:
    assert parse_heading("1. Ngân hàng phải...", inside_article=False) is None
    heading = parse_heading("1. Ngân hàng phải...", inside_article=True)
    assert heading is not None
    assert heading.label == "Khoản 1"


def test_points_use_vietnamese_letter_ordering() -> None:
    heading = parse_heading("đ) Trường hợp khác", inside_article=True)
    assert heading is not None
    assert heading.label == "Điểm đ"


def test_tracker_builds_the_path_and_closes_siblings() -> None:
    tracker = SectionTracker()
    for line in ["Chương II", "Điều 12. Tài sản có rủi ro", "2. Hệ số rủi ro"]:
        tracker.feed(line)
    assert tracker.path == ["Chương II", "Điều 12", "Khoản 2"]

    # A new article closes the previous article's clauses...
    tracker.feed("Điều 13. Vốn tự có")
    assert tracker.path == ["Chương II", "Điều 13"]
    # ...and a new chapter closes everything below it.
    tracker.feed("Chương III")
    assert tracker.path == ["Chương III"]


def test_tracker_reports_the_current_article() -> None:
    tracker = SectionTracker()
    tracker.feed("Chương I")
    assert tracker.current_article is None
    tracker.feed("Điều 7. Phạm vi")
    assert tracker.current_article is not None
    assert tracker.current_article.label == "Điều 7"


@pytest.mark.parametrize(
    ("path", "legal_number", "expected"),
    [
        (
            ["Chương II", "Điều 12", "Khoản 2"],
            "TT 41/2016/TT-NHNN",
            "Điều 12.2, TT 41/2016/TT-NHNN",
        ),
        (["Điều 12"], "TT 41/2016/TT-NHNN", "Điều 12, TT 41/2016/TT-NHNN"),
        (["Chương II", "Điều 12", "Khoản 2", "Điểm a"], None, "Điều 12.2a"),
        (["Mục 3"], "QĐ 114/2023", "Mục 3, QĐ 114/2023"),
        ([], "QĐ 114/2023", "QĐ 114/2023"),
    ],
)
def test_citation_labels_read_the_way_lawyers_write_them(
    path: list[str], legal_number: str | None, expected: str
) -> None:
    assert build_citation_label(path, legal_number) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% theo quy định.", "vi"),
        ("The bank shall maintain a minimum capital adequacy ratio of 8 percent.", "en"),
        (
            "Ngân hàng phải duy trì tỷ lệ an toàn vốn. The internal buffer is set at "
            "150 bps above the regulatory minimum and shall be reviewed by the committee.",
            "mixed",
        ),
        ("Điều 12", "vi"),
        ("", "unknown"),
    ],
)
def test_language_detection(text: str, expected: str) -> None:
    assert detect_language(text) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (["Chương II", "Điều 12", "Khoản 2"], "12.2"),
        (["Điều 12"], "12"),
        (["Chương II", "Điều 12", "Khoản 2", "Điểm a"], "12.2a"),
        (["Điều 12a"], "12a"),
        # A point with no clause above it: "12a" would be indistinguishable from Điều 12a,
        # and an anchor is a join key — an ambiguous one resolves to the wrong text silently.
        (["Điều 12", "Điểm a"], "12"),
        # Not an article at all. A procedure's step is a real citation and not an anchor.
        (["Mục 3"], None),
        (["Bước 3"], None),
        ([], None),
    ],
)
def test_the_anchor_is_the_bare_dotted_address(path: list[str], expected: str | None) -> None:
    assert build_anchor(path) == expected


def test_the_anchor_and_the_label_are_the_same_address() -> None:
    """The label is the anchor dressed for a reader. If these ever disagree about which clause
    they name, a reference resolves to text the citation does not point at."""
    path = ["Chương II", "Điều 12", "Khoản 2"]
    assert build_citation_label(path).replace("Điều ", "") == build_anchor(path)


def test_an_english_article_keeps_its_own_label() -> None:
    """The corpus is bilingual: a citation has to read back in the language it was written
    in, while the anchor it resolves through is language-free."""
    path = ["Chapter II", "Article 12", "Khoản 2"]
    assert build_citation_label(path) == "Article 12.2"
    assert build_anchor(path) == "12.2"
