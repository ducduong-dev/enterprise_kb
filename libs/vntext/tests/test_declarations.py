"""Reading the sentence in which an instrument says what it replaces (M9c, ADR-0039).

Recall is the number that matters here. A missed declaration does not vanish — it falls through
to the inference funnel, where it costs a model call and arrives with weaker evidence — but the
whole value of this path is that it is cheap and certain, and recall is the only thing that says
whether it still is. So the fixtures below are shapes taken from real closing articles, and a
shape that stops being read is meant to fail loudly here.

The second half is about *not* reading things: a citation is not a declaration, and a sentence
that merely enacts under an instrument must not be read as ending it.
"""

from __future__ import annotations

from datetime import date

import pytest
from kb_vntext.supersession import Declaration, find_declarations


def _one(text: str, **kwargs: str | None) -> Declaration:
    found = find_declarations(text, **kwargs)
    assert len(found) == 1, f"expected exactly one declaration, got {found}"
    return found[0]


# ------------------------------------------------------------------ the four shapes


def test_a_whole_instrument_replacement() -> None:
    d = _one(
        "Thông tư này có hiệu lực thi hành kể từ ngày 01/01/2027 và thay thế "
        "Thông tư 10/2022/TT-NHNN ngày 15/3/2022."
    )
    assert d.kind == "replaces"
    assert d.target is not None and d.target.value == "10/2022/TT-NHNN"
    assert d.target_anchors == [], "no article named means the whole instrument"
    assert d.effective_from == date(2027, 1, 1)


def test_a_clause_abrogated_with_nothing_in_its_place() -> None:
    d = _one("Bãi bỏ khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN.")
    assert d.kind == "abrogates"
    assert d.target_anchors == ["12.2"], "one clause ends; the other three stand"
    assert d.ends_without_replacement


def test_an_article_amended_in_the_passive() -> None:
    """The shape a consolidating amendment uses for every article it touches. Vietnamese puts
    the verb after the subject here, so a backward-only cue search misses most amendments."""
    d = _one("Điều 5 Thông tư 10/2022/TT-NHNN được sửa đổi, bổ sung như sau:")
    assert d.kind == "amends"
    assert d.target_anchors == ["5"]


def test_both_ends_named_with_the_direction() -> None:
    """The reason this path needs no inference: the sentence says which side is which, so
    nothing is decided from dates or publish order."""
    d = _one("Điều 7 Thông tư này thay thế Điều 5 Thông tư 10/2022/TT-NHNN.")
    assert d.kind == "replaces"
    assert d.target_anchors == ["5"], "the anchor before the instrument number is the target"
    assert d.replacement_anchors == ["7"], "the anchor before 'này' is the replacement"


def test_an_instrument_that_simply_ceases() -> None:
    """No passive marker, because an instrument does not "hết hiệu lực" something."""
    d = _one("Thông tư 10/2022/TT-NHNN hết hiệu lực kể từ ngày 01/01/2027.")
    assert d.kind == "abrogates"
    assert d.effective_from == date(2027, 1, 1)


def test_a_closing_article_declares_several_changes_at_once() -> None:
    """One paragraph, a dozen changes — which is why confirmation is a batch screen per
    declaring document rather than one pair at a time (ADR-0039)."""
    article = (
        "Điều 3. Hiệu lực thi hành\n"
        "1. Thông tư này có hiệu lực kể từ ngày 01/01/2027.\n"
        "2. Bãi bỏ khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN.\n"
        "3. Điều 5 Thông tư 11/2021/TT-NHNN được sửa đổi, bổ sung như sau:\n"
        "4. Thông tư này thay thế Quyết định 1627/2001/QĐ-NHNN.\n"
    )
    found = find_declarations(article)

    assert {d.target.value for d in found if d.target} == {
        "10/2022/TT-NHNN",
        "11/2021/TT-NHNN",
        "1627/2001/QĐ-NHNN",
    }
    assert {d.kind for d in found} == {"abrogates", "amends", "replaces"}


# ---------------------------------------------------------------- what it must not read


def test_an_ordinary_citation_is_not_a_declaration() -> None:
    """Every instrument cites others constantly. Reading a citation as a repeal would put a
    proposal in a steward's queue for every reference in the corpus."""
    assert find_declarations("Theo quy định tại Điều 12 Thông tư 41/2016/TT-NHNN.") == []
    assert find_declarations("Thông tư này quy định về tỷ lệ an toàn vốn.") == []


def test_the_instrument_a_document_is_enacted_under_is_not_repealed_by_it() -> None:
    """ "Căn cứ Thông tư X, nay bãi bỏ Điều 5 Thông tư Y" — the verb after X belongs to Y. A
    forward cue search without the passive test declares the enabling instrument dead."""
    found = find_declarations(
        "Căn cứ Thông tư 10/2022/TT-NHNN, nay bãi bỏ Điều 5 Thông tư 15/2020/TT-NHNN."
    )
    assert [d.target.value for d in found if d.target] == ["15/2020/TT-NHNN"]


def test_a_document_does_not_declare_against_itself() -> None:
    """Vietnamese instruments name themselves constantly, and a document superseding itself is
    a renumbering — the merge flow's business, not this one's."""
    text = "Thông tư 15/2025/TT-NHNN này thay thế Thông tư 15/2025/TT-NHNN."
    assert find_declarations(text, own_number="15/2025/TT-NHNN") == []


def test_the_targets_own_issue_date_is_not_the_declarations_date() -> None:
    """The failure that would matter most: "thay thế Thông tư 10/2022 ngày 15/3/2022" dated by
    the first number in the sentence takes effect years early, in the direction that keeps a
    repealed rule in service."""
    d = _one("Thông tư này thay thế Thông tư 10/2022/TT-NHNN ngày 15/3/2022.")
    assert d.effective_from is None, "no effectivity cue, so no date is claimed"


def test_a_declaration_carries_the_sentence_it_was_read_from() -> None:
    """Confirming one is a glance rather than a document read (ADR-0029's discipline)."""
    d = _one("Bãi bỏ khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN.")
    assert "Bãi bỏ khoản 2 Điều 12" in d.evidence
    assert d.confidence > 0.9


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Điều 1. Phạm vi điều chỉnh",
        "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8%.",
    ],
)
def test_a_document_that_declares_nothing_yields_nothing(text: str) -> None:
    assert find_declarations(text) == []
