"""Deterministic PII detectors for a Vietnamese bank's document corpus.

These are the rules the gate can never be wrong about in the direction that matters: a
detection here is a hard block, no model involved, no network call. They exist because the
documents that must never be published — a branch's customer list, a complaint file with an
account number in it, a training deck built from real data — are recognisable by their shape.

The design tension throughout is **context versus recall**. A bare 12-digit number is a CCCD,
a transaction reference, a legal-document number or a page of a table. Matching all of them
blocks the entire corpus; matching none of them lets a customer's identity number through.
Each rule below therefore states what it requires *around* the number, and why.

False positives cost a reviewer's time and are visible. False negatives publish a customer's
account number to the whole bank and are invisible. Every threshold here is set accordingly.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from kb_ports.models import PiiFinding

#: How much text either side of a match is inspected for the words that give it meaning.
CONTEXT_WINDOW = 60


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    confidence: float
    #: Words that must appear near the match for it to count. Empty means the shape is
    #: sufficient on its own.
    requires_context: tuple[str, ...] = ()
    #: Words that, when present, mean this is not PII after all.
    excluded_by: tuple[str, ...] = ()
    description: str = ""


# --------------------------------------------------------------------------------- shapes

#: Căn cước công dân: exactly 12 digits. The first three are a province code, which is what
#: separates a CCCD from any other 12-digit run — a real discriminator, not a guess.
_CCCD = re.compile(r"(?<!\d)(\d{12})(?!\d)")
#: Chứng minh nhân dân, the pre-2016 identity number: 9 digits. Far too common a shape to
#: block on its own, so it requires context words.
_CMND = re.compile(r"(?<!\d)(\d{9})(?!\d)")
#: Payment card numbers, 13 to 19 digits with optional separators. Luhn-validated below.
_PAN = re.compile(r"(?<!\d)((?:\d[ -]?){12,18}\d)(?!\d)")
#: Vietnamese mobile and landline numbers, with or without the +84 country code.
_PHONE = re.compile(r"(?<![\d+])(?:\+?84|0)(?:\d[ .-]?){8,10}\d(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.UNICODE)
#: Ma so thue (tax code): 10 digits, optionally with a 3-digit branch suffix.
_TAX_CODE = re.compile(r"(?<!\d)(\d{10}(?:-\d{3})?)(?!\d)")
#: Account numbers as banks actually write them: 6 to 19 digits, no separators.
_ACCOUNT = re.compile(r"(?<!\d)(\d{6,19})(?!\d)")
#: Money amounts in Vietnamese notation: 1.234.567 VND / 1,234,567 đồng.
_MONEY = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d{3})+|\d{7,})\s*(?:VND|VNĐ|đồng|đ)\b", re.IGNORECASE)
#: A personal name in a document that also carries a money amount. Vietnamese names are
#: three or four capitalised syllables; this is the "name + balance" heuristic the plan asks
#: for, and it is the one rule here that is genuinely a heuristic.
_PERSON_NAME = re.compile(
    r"\b(?:Ông|Bà|Anh|Chị|Khách hàng|Chủ tài khoản|Họ và tên|Họ tên)\s*:?\s*"
    r"((?:[A-ZĐÂÊÔƠƯÁÀẢÃẠÉÈẺẼẸÍÌỈĨỊÓÒỎÕỌÚÙỦŨỤÝỲỶỸỴ][^\W\d_]+\s+){1,3}"
    r"[A-ZĐÂÊÔƠƯÁÀẢÃẠÉÈẺẼẸÍÌỈĨỊÓÒỎÕỌÚÙỦŨỤÝỲỶỸỴ][^\W\d_]+)"
)
#: Dates of birth, which turn a name into an identifiable person.
_DOB = re.compile(
    r"(?:ngày sinh|sinh ngày|năm sinh|date of birth|DOB)\s*:?\s*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4})",
    re.IGNORECASE,
)
#: Residential address: an address label, then a house number, then a street/ward/district
#: word. The label and the number are allowed a few words apart — "Địa chỉ giao thẻ: số 148
#: đường ..." is an address, and demanding they be adjacent misses most real ones.
_ADDRESS = re.compile(
    r"(?:địa chỉ|thường trú|nơi ở|nơi cư trú)[^\n]{0,20}?:?\s*"
    r"(?:số\s*)?\d+[^\n,]{0,40}?"
    r"(?:đường|phố|phường|xã|quận|huyện|thị trấn|ngõ)\b[^\n]{0,60}",
    re.IGNORECASE,
)

#: "tài khoản" on its own is enough. Requiring the contiguous phrase "số tài khoản" missed
#: "Tài khoản nghi vấn số 19008877665544" — the way a fraud note actually reads — and the
#: rule still needs a 6-to-19-digit run with no separators nearby, which ordinary prose about
#: accounts does not contain.
ACCOUNT_CONTEXT = (
    "tài khoản", "so tai khoan", "tai khoan", "stk", "account", "account number", "số tk",
)  # fmt: skip
CARD_CONTEXT = ("thẻ", "card", "pan", "số thẻ")
IDENTITY_CONTEXT = (
    "cccd", "căn cước", "can cuoc", "cmnd", "chứng minh nhân dân", "chung minh",
    "giấy tờ tùy thân", "identity", "id number", "số định danh",
)  # fmt: skip
TAX_CONTEXT = ("mã số thuế", "ma so thue", "mst", "tax code", "tax id")
PHONE_CONTEXT = (
    "điện thoại", "dien thoai", "số điện thoại", "sđt", "sdt", "phone", "mobile", "liên hệ",
)  # fmt: skip

#: Contexts in which a number that looks like PII is something else entirely. These are the
#: false positives that would otherwise block half the regulatory corpus.
#:
#: Deliberately narrow. "khoản" belongs to this vocabulary — Khoản 2 is a clause — but it is
#: also the second half of "tài khoản", so listing it excluded every account number by way of
#: its own label. Ambiguous single words are left out; the account rule already requires an
#: account label, so an article number cannot reach it anyway.
LEGAL_NUMBER_CONTEXT = (
    "thông tư", "nghị định", "quyết định", "công văn", "phụ lục", "số hiệu",
    "circular", "decree",
)  # fmt: skip

#: Role mailboxes. A procedure telling staff to write to the risk team is not a disclosure of
#: anybody's personal address, and blocking it would train reviewers to override on reflex.
ROLE_MAILBOXES = (
    "risk", "support", "info", "contact", "hotline", "cskh", "admin", "noreply",
    "no-reply", "help", "service", "compliance", "legal", "it", "hr",
)  # fmt: skip
STATISTICAL_CONTEXT = (
    "tổng cộng", "tổng số", "trung bình", "tỷ lệ", "chỉ tiêu", "hạn mức", "định mức",
    "toàn hệ thống", "toàn ngành", "bình quân",
)  # fmt: skip


RULES: tuple[Rule, ...] = (
    Rule(
        kind="cccd",
        pattern=_CCCD,
        confidence=0.95,
        description="Căn cước công dân (12 digits with a valid province code)",
    ),
    Rule(
        kind="cmnd",
        pattern=_CMND,
        confidence=0.85,
        requires_context=IDENTITY_CONTEXT,
        description="Chứng minh nhân dân (9 digits, requires identity context)",
    ),
    Rule(
        kind="pan",
        pattern=_PAN,
        confidence=0.99,
        description="Payment card number (Luhn-valid)",
    ),
    Rule(
        kind="account_number",
        pattern=_ACCOUNT,
        confidence=0.90,
        requires_context=ACCOUNT_CONTEXT,
        excluded_by=LEGAL_NUMBER_CONTEXT,
        description="Bank account number, identified by its label",
    ),
    Rule(
        kind="tax_code",
        pattern=_TAX_CODE,
        confidence=0.85,
        requires_context=TAX_CONTEXT,
        description="Mã số thuế",
    ),
    Rule(
        kind="phone",
        pattern=_PHONE,
        confidence=0.80,
        requires_context=PHONE_CONTEXT,
        description="Vietnamese phone number, identified by its label",
    ),
    Rule(kind="email", pattern=_EMAIL, confidence=0.75, description="Email address"),
    Rule(
        kind="date_of_birth",
        pattern=_DOB,
        confidence=0.80,
        description="Date of birth",
    ),
    Rule(
        kind="address",
        pattern=_ADDRESS,
        confidence=0.75,
        description="Residential address",
    ),
)


def luhn_valid(digits: str) -> bool:
    """The check digit that separates a card number from any other run of digits.

    Without it, every long number in a fee table is a card. With it, the false-positive rate
    is one in ten — and the rule is only applied to 13-19 digit runs to begin with.
    """
    stripped = [int(ch) for ch in digits if ch.isdigit()]
    if not 13 <= len(stripped) <= 19:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(stripped)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


#: Province codes issued for CCCD (001 to 096, non-contiguous). A 12-digit number whose first
#: three digits are not one of these is not a citizen identity number.
CCCD_PROVINCE_CODES: frozenset[str] = frozenset(
    f"{code:03d}"
    for code in (
        *range(1, 9),
        10,
        11,
        12,
        14,
        15,
        17,
        19,
        20,
        22,
        24,
        25,
        26,
        27,
        30,
        31,
        33,
        34,
        35,
        36,
        37,
        38,
        40,
        42,
        44,
        45,
        46,
        48,
        49,
        51,
        52,
        54,
        56,
        58,
        60,
        62,
        64,
        66,
        67,
        68,
        70,
        72,
        74,
        75,
        77,
        79,
        80,
        82,
        83,
        84,
        86,
        87,
        89,
        91,
        92,
        93,
        94,
        95,
        96,
    )
)


def cccd_valid(digits: str) -> bool:
    """A CCCD encodes province, gender/century and birth year — all checkable."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    if digits[:3] not in CCCD_PROVINCE_CODES:
        return False
    # The fourth digit encodes century and gender; 0-5 cover the 20th and 21st centuries.
    return int(digits[3]) <= 5


def scan_text(text: str, *, block_id: str | None = None) -> list[PiiFinding]:
    """Run every rule over `text`. Deterministic, offline, and the gate's hard floor."""
    findings: list[PiiFinding] = []
    lowered = text.lower()

    for rule in RULES:
        for match in rule.pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            if not _accepted(rule, value, lowered, match.start(), match.end()):
                continue
            findings.append(
                PiiFinding(
                    kind=rule.kind,
                    text=value,
                    start=match.start(),
                    end=match.end(),
                    confidence=rule.confidence,
                    detector=f"pattern:{rule.kind}",
                    block_id=block_id,
                )
            )

    findings.extend(_name_with_money(text, block_id))
    return _deduplicate(findings)


def _contains(context: str, phrase: str) -> bool:
    """Whole-word containment.

    Substring matching is what made "tài khoản" look like the clause marker "khoản"; word
    boundaries are the minimum, and ambiguous single words are kept out of the lists above.
    """
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", context) is not None


def _accepted(rule: Rule, value: str, lowered: str, start: int, end: int) -> bool:
    """Shape is necessary but rarely sufficient — this is where context decides."""
    context = lowered[max(0, start - CONTEXT_WINDOW) : end + CONTEXT_WINDOW]

    if rule.kind == "pan" and not luhn_valid(value):
        return False
    if rule.kind == "cccd" and not cccd_valid(value):
        return False
    if rule.kind == "email":
        local = value.split("@", 1)[0].lower()
        if local in ROLE_MAILBOXES:
            return False
    # A statistic is not an account. "Tổng số 12345678 giao dịch" must not block a management
    # report, and blocking it teaches reviewers to override on reflex.
    if rule.kind == "account_number" and any(
        _contains(context, word) for word in STATISTICAL_CONTEXT
    ):
        return False
    if rule.requires_context and not any(
        _contains(context, word) for word in rule.requires_context
    ):
        return False
    return not (rule.excluded_by and any(_contains(context, word) for word in rule.excluded_by))


def _name_with_money(text: str, block_id: str | None) -> Iterator[PiiFinding]:
    """A named person next to a money amount — a balance, a salary, a loan.

    Neither half is PII alone: policies name officers, and every fee schedule quotes amounts.
    Together, in the same paragraph, they identify what one person has, which is exactly the
    disclosure this gate exists to prevent.
    """
    amounts = [match.start() for match in _MONEY.finditer(text)]
    if not amounts:
        return

    for match in _PERSON_NAME.finditer(text):
        name = match.group(1)
        if any(abs(position - match.start()) <= 200 for position in amounts):
            yield PiiFinding(
                kind="name_with_balance",
                text=name,
                start=match.start(),
                end=match.end(),
                confidence=0.70,
                detector="pattern:name_with_balance",
                block_id=block_id,
            )


def _deduplicate(findings: list[PiiFinding]) -> list[PiiFinding]:
    """Keep the most confident finding for each span.

    A card number matches both `pan` and `account_number`; reporting it twice makes the
    reviewer's list longer without making it more informative.
    """
    best: dict[tuple[int, int], PiiFinding] = {}
    for finding in findings:
        key = (finding.start, finding.end)
        current = best.get(key)
        if current is None or finding.confidence > current.confidence:
            best[key] = finding

    kept: list[PiiFinding] = []
    for finding in sorted(best.values(), key=lambda item: (item.start, -item.confidence)):
        # Drop findings entirely contained in a stronger neighbouring one.
        if any(
            other.start <= finding.start and finding.end <= other.end and other is not finding
            for other in kept
        ):
            continue
        kept.append(finding)
    return kept


def redact_text(text: str, findings: list[PiiFinding]) -> str:
    """Replace each finding with a typed placeholder.

    Typed, not blanked: "[CCCD]" tells a reader that an identity number was removed, which is
    information a compliance officer needs and a bare "***" destroys.
    """
    if not findings:
        return text
    result = text
    for finding in sorted(findings, key=lambda item: item.start, reverse=True):
        result = result[: finding.start] + f"[{finding.kind.upper()}]" + result[finding.end :]
    return result
