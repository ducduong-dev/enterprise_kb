"""Four-eyes, as a rule rather than as a button (INV-8)."""

from __future__ import annotations

import pytest
from kb_workflows.merge_policy import (
    REGULATED_APPROVALS,
    STANDARD_APPROVALS,
    Approval,
    ApprovalError,
    ApprovalLedger,
    required_approvals,
)


def ledger(doc_class: str = "regulatory", author: str = "u-preparer") -> ApprovalLedger:
    return ApprovalLedger(doc_class=doc_class, author=author, draft_ref="kb-derived/draft-1")


@pytest.mark.parametrize("doc_class", ["regulatory", "customer_facing"])
def test_regulated_classes_need_two_approvers(doc_class: str) -> None:
    assert required_approvals(doc_class) == REGULATED_APPROVALS


@pytest.mark.parametrize("doc_class", ["operational", "internal_normative"])
def test_other_classes_need_one(doc_class: str) -> None:
    assert required_approvals(doc_class) == STANDARD_APPROVALS


def test_an_unknown_class_is_treated_as_regulated() -> None:
    """The safe direction: more eyes, not fewer."""
    assert required_approvals("something_new") == REGULATED_APPROVALS


def test_the_preparer_may_not_approve_their_own_merge() -> None:
    book = ledger()
    with pytest.raises(ApprovalError, match="preparer"):
        book.add(Approval(approver="u-preparer"))
    assert not book.satisfied


def test_one_person_cannot_approve_twice() -> None:
    """Two clicks are not two pairs of eyes."""
    book = ledger()
    book.add(Approval(approver="u-legal-one"))
    with pytest.raises(ApprovalError, match="already approved"):
        book.add(Approval(approver="u-legal-one"))
    assert not book.satisfied


def test_two_distinct_approvers_satisfy_a_regulatory_merge() -> None:
    book = ledger()
    book.add(Approval(approver="u-legal-one"))
    assert not book.satisfied
    book.add(Approval(approver="u-legal-two"))
    assert book.satisfied
    assert book.status()["approvers"] == ["u-legal-one", "u-legal-two"]


def test_one_approver_satisfies_an_operational_merge() -> None:
    book = ledger(doc_class="operational")
    book.add(Approval(approver="u-steward"))
    assert book.satisfied


def test_an_approval_for_a_superseded_draft_does_not_count() -> None:
    """They approved text that is no longer what would publish."""
    book = ledger()
    with pytest.raises(ApprovalError, match="draft changed"):
        book.add(Approval(approver="u-legal-one", draft_ref="kb-derived/draft-0"))
    assert not book.satisfied


def test_an_approval_without_a_draft_reference_is_accepted() -> None:
    """Older clients do not send one; the ledger does not invent a failure."""
    book = ledger()
    book.add(Approval(approver="u-legal-one"))
    assert book.status()["received"] == 1


def test_a_rejection_stops_the_merge() -> None:
    book = ledger()
    book.reject("u-legal-one", "Bản hợp nhất bỏ sót Khoản 3 Điều 12.")
    assert book.rejected
    assert not book.satisfied
    with pytest.raises(ApprovalError, match="already been rejected"):
        book.add(Approval(approver="u-legal-two"))


def test_the_status_explains_what_is_still_missing() -> None:
    """The merge screen renders this; "pending" without a count is useless to a reviewer."""
    book = ledger()
    book.add(Approval(approver="u-legal-one", note="Đã đối chiếu Điều 6 và Điều 12."))
    status = book.status()
    assert status["required"] == 2
    assert status["received"] == 1
    assert status["satisfied"] is False
