"""How many people must agree before a merged document is published.

Pure functions, so the rule that actually matters — a regulatory consolidation needs two
approvers, neither of them the person who prepared it — is testable without a workflow
harness, and is stated in one place rather than inferred from workflow code.

The rule is not "an approve button exists". Four-eyes fails in practice through three specific
holes, and each is closed here:

* the preparer approving their own work;
* one person approving twice (two clicks, one pair of eyes);
* an approval arriving for a draft that has since changed underneath it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kb_schemas.enums import NO_AUTOMATION_CLASSES, DocClass

#: Regulatory and customer-facing documents need a second approver as well as the preparer.
#: Everything else needs one person who is not the author.
REGULATED_APPROVALS = 2
STANDARD_APPROVALS = 1


def required_approvals(doc_class: str) -> int:
    """INV-8: regulated classes never publish on one person's say-so."""
    try:
        parsed = DocClass(doc_class)
    except ValueError:  # pragma: no cover - the column is an enum
        return REGULATED_APPROVALS
    return REGULATED_APPROVALS if parsed in NO_AUTOMATION_CLASSES else STANDARD_APPROVALS


@dataclass(frozen=True, slots=True)
class Approval:
    approver: str
    note: str = ""
    #: Identifies the draft the approver actually read. An approval for a superseded draft is
    #: an approval of something that no longer exists.
    draft_ref: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalError(Exception):
    reason: str

    def __str__(self) -> str:  # pragma: no cover - message plumbing
        return self.reason


@dataclass
class ApprovalLedger:
    """Accumulates approvals for one merge and decides when it may publish."""

    doc_class: str
    author: str
    draft_ref: str
    approvals: list[Approval] = field(default_factory=list)
    rejected_by: str | None = None
    rejection_reason: str = ""

    @property
    def required(self) -> int:
        return required_approvals(self.doc_class)

    @property
    def approvers(self) -> set[str]:
        return {approval.approver for approval in self.approvals}

    @property
    def satisfied(self) -> bool:
        return self.rejected_by is None and len(self.approvers) >= self.required

    @property
    def rejected(self) -> bool:
        return self.rejected_by is not None

    def add(self, approval: Approval) -> None:
        """Record an approval, or explain why it does not count."""
        if self.rejected_by is not None:
            raise ApprovalError("this merge has already been rejected")
        if approval.approver == self.author:
            # The whole point of four-eyes. A preparer approving their own consolidation is
            # one pair of eyes wearing two hats.
            raise ApprovalError("the preparer may not approve their own merge (INV-8)")
        if approval.approver in self.approvers:
            raise ApprovalError("this person has already approved; a second approver is needed")
        if approval.draft_ref and approval.draft_ref != self.draft_ref:
            # The draft was regenerated after they read it. Their approval was for text that
            # is no longer what would publish.
            raise ApprovalError("the draft changed after this approval was given")
        self.approvals.append(approval)

    def reject(self, approver: str, reason: str) -> None:
        self.rejected_by = approver
        self.rejection_reason = reason

    def status(self) -> dict[str, object]:
        return {
            "required": self.required,
            "received": len(self.approvers),
            "approvers": sorted(self.approvers),
            "satisfied": self.satisfied,
            "rejected_by": self.rejected_by,
            "rejection_reason": self.rejection_reason,
        }
