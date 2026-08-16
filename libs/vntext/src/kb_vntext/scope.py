"""Who and what a clause applies to, as the text states it.

`different_scope` is the expensive false positive in clause-supersession detection: two clauses
about lending rates that state different rates look identical to every similarity measure, and
one is for individuals while the other is for corporates. Confirming that pair removes a correct
answer from search.

In this corpus scope is usually **stated, not implied** — *"áp dụng đối với khách hàng cá
nhân"*, *"cho vay ngắn hạn bằng đồng Việt Nam"* — so the dangerous bucket becomes deterministic
wherever the text was explicit, and only reaches a model where it was not (ADR-0033, gate 3).

Patterns only, ADR-0013's floor.

**Silence is not a scope.** A clause that names no segment is not thereby "all segments"; it is
a clause whose segment we do not know. So a facet counts only when *both* sides state one, and
`conflict` returns nothing on a facet either side left out. Reading silence as universality
would make every unqualified clause conflict with every qualified one, which is the opposite of
the error this gate exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from kb_vntext.language import fold_diacritics

#: Facet → canonical value → the spellings that mean it, diacritic-folded and lowercased.
#:
#: Values inside one facet are mutually exclusive *as written*: a clause for cá nhân is not a
#: clause for doanh nghiệp. That exclusivity is what makes a disjoint pair evidence, and it is
#: why the vocabulary is small and specific rather than a general keyword list — "khách hàng"
#: appears in both and says nothing.
_FACETS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "segment": {
        "cá nhân": ("ca nhan", "individual", "retail customer", "personal customer"),
        "doanh nghiệp": (
            "doanh nghiep", "to chuc kinh te", "khach hang doanh nghiep",
            "corporate", "enterprise", "business customer",
        ),
        "ưu tiên": ("uu tien", "priority", "vip"),
        "tổ chức tín dụng": ("to chuc tin dung", "credit institution"),
    },
    "currency": {
        "VND": ("dong viet nam", "vnd", "noi te", "vietnamese dong"),
        "ngoại tệ": ("ngoai te", "foreign currency", "usd", "eur"),
    },
    "term": {
        "không kỳ hạn": ("khong ky han", "demand deposit", "current account"),
        "ngắn hạn": ("ngan han", "short term", "short-term"),
        "trung hạn": ("trung han", "medium term", "medium-term"),
        "dài hạn": ("dai han", "long term", "long-term"),
    },
    "channel": {
        "quầy": ("tai quay", "quay giao dich", "over the counter", "branch counter"),
        "điện tử": (
            "ngan hang dien tu", "internet banking", "mobile banking", "truc tuyen", "online",
        ),
        "ATM": ("atm", "may rut tien"),
    },
    "product": {
        "tiền gửi": ("tien gui", "deposit"),
        "cho vay": ("cho vay", "tin dung", "loan", "lending"),
        "thẻ": ("the tin dung", "the ghi no", "credit card", "debit card"),
        "bảo lãnh": ("bao lanh", "guarantee"),
        "chuyển tiền": ("chuyen tien", "remittance", "funds transfer"),
    },
}  # fmt: skip

#: The applicability clause, which is where scope is stated when it is stated deliberately.
#: Matched only to *weight* a reading, never to require one: plenty of clauses carry their scope
#: in the ordinary run of the sentence.
_APPLICABILITY = re.compile(
    r"(?:áp\s+dụng\s+(?:đối\s+với|cho)|ap\s+dung\s+(?:doi\s+voi|cho)|applies?\s+to"
    r"|dành\s+cho|danh\s+cho)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Scope:
    """What a clause says about its own applicability."""

    facets: dict[str, frozenset[str]] = field(default_factory=dict)
    #: True when the text carries an explicit applicability clause. Not required for a facet to
    #: count — it raises confidence, and the review screen shows it.
    stated: bool = False

    def __bool__(self) -> bool:
        return bool(self.facets)

    def as_dict(self) -> dict[str, list[str]]:
        return {facet: sorted(values) for facet, values in sorted(self.facets.items())}


def find_scope(text: str) -> Scope:
    """The scope facets this text names.

    Folded and lowercased before matching, so a document that lost its tone marks to OCR still
    reads — unlike the quantity extractor, no value here collides with a common word, because
    the vocabulary is deliberately specific.
    """
    folded = fold_diacritics(text).lower()
    found: dict[str, frozenset[str]] = {}
    for facet, values in _FACETS.items():
        matched = {
            canonical
            for canonical, spellings in values.items()
            if any(spelling in folded for spelling in spellings)
        }
        if matched:
            found[facet] = frozenset(matched)
    return Scope(facets=found, stated=bool(_APPLICABILITY.search(text)))


def conflict(left: Scope, right: Scope) -> dict[str, dict[str, list[str]]]:
    """Facets on which these two clauses cannot both be about the same rule.

    A facet conflicts when both sides state one and the stated values are **disjoint**. Not
    merely different: a clause for `{cá nhân}` and a clause for `{cá nhân, doanh nghiệp}`
    overlap, and the second is the broader statement of the same rule rather than a rule about
    somebody else — treating that as a conflict would let a widening amendment escape detection.

    A facet only one side mentions is reported as `unstated` rather than as agreement or
    conflict. It is genuinely unknown, and a steward looking at the pair should see which.

    An empty `mismatched` is what lets gate 3 pass a pair through to the next gate; a non-empty
    one is `different_scope` recorded with no model call (ADR-0033).
    """
    mismatched: dict[str, list[str]] = {}
    matched: dict[str, list[str]] = {}
    unstated: dict[str, list[str]] = {}

    for facet in sorted(set(left.facets) | set(right.facets)):
        here = left.facets.get(facet)
        there = right.facets.get(facet)
        if here is None or there is None:
            stated = here if here is not None else there
            unstated[facet] = sorted(stated or ())
            continue
        shared = here & there
        if shared:
            matched[facet] = sorted(shared)
        else:
            mismatched[facet] = sorted(here | there)

    return {"matched": matched, "mismatched": mismatched, "unstated": unstated}


def differs(left: Scope, right: Scope) -> bool:
    """Whether gate 3 stops here. True means `different_scope`, with no model call."""
    return bool(conflict(left, right)["mismatched"])


__all__ = ["Scope", "conflict", "differs", "find_scope"]
