"""Gate 3: who and what a clause applies to (M9d, ADR-0033).

`different_scope` is the expensive false positive. Two clauses about lending rates that state
different rates are maximally similar to every measure the funnel has, and one is for
individuals while the other is for corporates — confirming that pair removes a correct answer
from search.

So the tests are about the three ways this gate can be wrong: missing a stated difference,
inventing one from silence, and calling a broader restatement a different rule.
"""

from __future__ import annotations

from kb_vntext.scope import conflict, differs, find_scope

INDIVIDUAL = "Lãi suất cho vay ngắn hạn bằng đồng Việt Nam đối với khách hàng cá nhân là 8%/năm."
CORPORATE = (
    "Lãi suất cho vay ngắn hạn bằng đồng Việt Nam đối với khách hàng doanh nghiệp là 10%/năm."
)
INDIVIDUAL_NEWER = (
    "Lãi suất cho vay ngắn hạn bằng đồng Việt Nam đối với khách hàng cá nhân là 10%/năm."
)


def test_the_facets_a_clause_states_are_read() -> None:
    scope = find_scope(INDIVIDUAL)
    assert scope.as_dict() == {
        "currency": ["VND"],
        "product": ["cho vay"],
        "segment": ["cá nhân"],
        "term": ["ngắn hạn"],
    }


def test_two_segments_are_a_different_rule() -> None:
    """The pair this gate exists for: same subject, same words, different customers. Recorded
    as `different_scope` with no model call."""
    assert differs(find_scope(INDIVIDUAL), find_scope(CORPORATE))
    assert conflict(find_scope(INDIVIDUAL), find_scope(CORPORATE))["mismatched"] == {
        "segment": ["cá nhân", "doanh nghiệp"]
    }


def test_the_same_scope_passes_through_to_the_next_gate() -> None:
    assert not differs(find_scope(INDIVIDUAL), find_scope(INDIVIDUAL_NEWER))


def test_silence_is_not_a_scope() -> None:
    """A clause naming no segment is not thereby "all segments" — it is a clause whose segment
    we do not know. Reading silence as universality would make every unqualified clause
    conflict with every qualified one, which is the opposite of this gate's job."""
    silent = find_scope("Lãi suất cho vay là 10%/năm.")

    assert not differs(find_scope(INDIVIDUAL), silent)
    reported = conflict(find_scope(INDIVIDUAL), silent)
    assert reported["mismatched"] == {}
    assert "segment" in reported["unstated"], "a steward should see which side was silent"


def test_a_broader_clause_does_not_conflict_with_the_narrower_one_it_covers() -> None:
    """A clause for {cá nhân} and one for {cá nhân, doanh nghiệp} overlap: the second is the
    broader statement of the same rule, not a rule about somebody else. Treating that as a
    conflict lets a widening amendment escape detection."""
    broad = find_scope("Áp dụng đối với khách hàng cá nhân và khách hàng doanh nghiệp.")

    assert not differs(find_scope(INDIVIDUAL), broad)
    assert conflict(find_scope(INDIVIDUAL), broad)["matched"]["segment"] == ["cá nhân"]


def test_currency_term_and_channel_each_separate_a_rule() -> None:
    assert differs(find_scope("Cho vay bằng đồng Việt Nam."), find_scope("Cho vay bằng ngoại tệ."))
    assert differs(find_scope("Tiền gửi ngắn hạn."), find_scope("Tiền gửi dài hạn."))
    assert differs(find_scope("Giao dịch tại quầy."), find_scope("Giao dịch qua internet banking."))


def test_scope_survives_lost_tone_marks() -> None:
    """OCR drops the marks. Unlike the quantity extractor's units, no value here collides with
    a common word, so the folded spellings are safe to accept."""
    assert find_scope("ap dung doi voi khach hang ca nhan").facets["segment"] == frozenset(
        {"cá nhân"}
    )


def test_an_applicability_clause_is_noted() -> None:
    """Not required for a facet to count — plenty of clauses carry scope in the ordinary run of
    the sentence — but it raises confidence and the review screen shows it."""
    assert find_scope("Áp dụng đối với khách hàng cá nhân.").stated
    assert not find_scope("Lãi suất cho vay cá nhân là 8%/năm.").stated
