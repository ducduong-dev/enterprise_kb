"""Context assembly and citation verification.

Two halves of one idea: what the model is allowed to see, and what the user is allowed to be
told it said.

**Assembly** numbers the retrieved passages and puts each one behind its citation label, within
a budget. Order matters — the highest-scoring passage is first, because that is where a model
looks — and so does the label, because the model is told to cite by number and the number is
what maps back to a version ID.

**Verification** is the half that does the work. A model asked to cite will sometimes cite
`[7]` when six passages were supplied, cite a passage that says nothing about its claim, or
answer with no citation at all. Each of those is caught here rather than shown to a compliance
officer:

* a marker with no passage behind it is stripped from the answer;
* a citation whose passage shares no substantive vocabulary with the sentence citing it is
  dropped, and the sentence keeps its text but loses the claim to authority;
* an answer left with no citations at all is not returned — the refusal is (INV-11 is about
  reconstructing answers; an unciteable answer cannot be reconstructed, so it is not an
  answer).

None of this makes a model truthful. It makes an untruthful one visible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from kb_common.logging import get_logger
from kb_schemas.api import Citation, RetrievedChunk

log = get_logger(__name__)

#: Roughly four characters per token for Vietnamese in this tokenizer family. Deliberately
#: pessimistic: overrunning the window truncates the *last* passage, which is the one the
#: model is most likely to cite as an afterthought.
CHARS_PER_TOKEN = 4
DEFAULT_CONTEXT_TOKENS = 3000
#: Below this overlap between a sentence and the passage it cites, the citation is treated as
#: unsupported. Low enough to tolerate paraphrase, high enough to catch a citation attached to
#: a sentence about something else entirely.
SUPPORT_THRESHOLD = 0.18
#: One word in common is a coincidence — "quy định" appears in every regulation ever written.
#: A sentence with three or more substantive words must share at least two with its passage.
MIN_SHARED_TERMS = 2
#: A quoted passage in the response, trimmed so the UI can show it inline.
QUOTE_CHARS = 400
#: Share of the question's substantive words the best passage must contain before that passage
#: counts as being *about* the question. Retrieval always returns its best guesses, even for a
#: question the corpus cannot answer; without this the bot answers "what is the daily ATM
#: limit?" by quoting the fee schedule, with a citation that verifies perfectly.
#:
#: Measured over the answer set on the seeded corpus: answerable questions score 0.53 to 1.00
#: (with one at 0.36 that is refused and counted as a miss), unanswerable ones 0.24 to 0.44.
#: The threshold sits in that gap. The margin is narrow, which is the honest state of a
#: lexical signal — in production the cross-encoder reranker is the relevance judgement and
#: this is only the floor beneath it. Questions where the *wording* is identical and only the
#: concept differs ("ATM withdrawal limit" against a passage about ATM withdrawal fees) are
#: out of reach of any lexical measure and are marked `requires: model` in the answer set.
RELEVANCE_RATIO = 0.48
#: How much a question word that appears in *no* retrieved passage counts against relevance.
ABSENT_TERM_WEIGHT = 0.5

_MARKER = re.compile(r"\[(\d{1,2})\]")
#: Numbers, with the separators Vietnamese regulatory text uses: 8%, 254.000.000, 0,5%.
_NUMBER = re.compile(r"\d[\d.,]*%?")
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n+")
_WORD = re.compile(r"\w+", re.UNICODE)
#: Vietnamese function words carry no evidence of support; a sentence and a passage sharing
#: only "của" and "là" share nothing.
_STOPWORDS = frozenset(
    """
    la va cua cho voi tu den ve theo tai trong ngoai tren duoi mot cac nhung nay do kia thi
    ma neu khi duoc phai co khong se da dang bi boi vi nen hoac cung chi ra vao the nao sao
    gi day day_la o toi ban chung ta ho no cai
    """.split()  # noqa: SIM905 — a word list stays readable as prose, not as 60 quoted items
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(_fold(text)) if w not in _STOPWORDS and len(w) > 1}


def _numbers(text: str) -> set[str]:
    """Every number a sentence asserts, normalized.

    Vocabulary overlap cannot tell "tỷ lệ tối thiểu là 8%" from "tỷ lệ tối thiểu là 12%" — the
    two sentences share every word that matters. In a bank the number *is* the claim, so a
    citation is only kept when the passage contains the figures the sentence quotes.
    """
    return {match.rstrip(".,") for match in _NUMBER.findall(text.replace(" ", ""))}


@dataclass(frozen=True, slots=True)
class ContextItem:
    marker: int
    chunk: RetrievedChunk

    @property
    def label(self) -> str:
        return self.chunk.citation_label or self.chunk.section_path or "Tài liệu"

    @property
    def heading(self) -> str:
        """What the model sees above the passage: the document, then the article."""
        title = self.chunk.document_title
        return f"{title} — {self.label}" if title else self.label

    @property
    def searchable(self) -> str:
        """Everything about the passage a relevance judgement may read.

        Includes the document title, which is often what makes a passage recognisably about a
        question ("Quy trình nhận biết khách hàng (KYC)" answers a question about KYC that its
        chunk text never spells out). The title also carries the most common words in the
        corpus, which is why the scoring below discounts terms that appear everywhere.
        """
        return f"{self.heading} {self.chunk.section_path or ''} {self.chunk.text}"


@dataclass
class AssembledContext:
    items: list[ContextItem] = field(default_factory=list)
    #: Passages dropped for budget. Surfaced as a warning: an answer built from half the
    #: evidence should say so.
    dropped: int = 0

    @property
    def empty(self) -> bool:
        return not self.items

    @property
    def superseded(self) -> bool:
        return any(item.chunk.supersession_flag for item in self.items)

    @property
    def replaced(self) -> bool:
        """Whether any passage here is a clause a confirmed newer one replaced.

        Different question from `superseded`, which asks whether the *document* has an
        unconsolidated amendment somewhere in it. This one is about the passage in hand.
        """
        return any(item.chunk.superseded_by is not None for item in self.items)

    def render(self) -> str:
        """The context block the prompt embeds. One passage per numbered section."""
        blocks = []
        for item in self.items:
            notes = _amendment_note(item) + _replacement_note(item)
            blocks.append(f"[{item.marker}] {item.heading}\n{item.chunk.text.strip()}{notes}")
        return "\n\n".join(blocks)

    def by_marker(self, marker: int) -> ContextItem | None:
        for item in self.items:
            if item.marker == marker:
                return item
        return None


def _amendment_note(item: ContextItem) -> str:
    """M5's document-level warning: an amendment exists and nobody has consolidated it."""
    if not item.chunk.supersession_flag:
        return ""
    return "\n[CẢNH BÁO] Văn bản này đang có văn bản sửa đổi chưa được hợp nhất."


def _replacement_note(item: ContextItem) -> str:
    """M9's clause-level pointer: *this* passage was replaced, and here is by what.

    **The replacement is never one of the numbered passages.** Fusion drops a superseded clause
    whenever its replacement was retrieved too, so a clause that reaches the context carrying
    this note is one whose replacement is *not* here — which is exactly why the note has to
    name it in prose. The prompt tells the model not to invent a marker for it; `verify` strips
    one if it does.

    Two forms, because `SupersededBy` has two. Where the caller may read the replacement it is
    named, so the answer can point somewhere. Where they may not, the date is all there is, and
    the note says so plainly rather than hinting at a document the reader cannot reach — the
    model is told to advise checking the current version instead of speculating about it.
    """
    pointer = item.chunk.superseded_by
    if pointer is None:
        return ""
    when = pointer.supersedes_from.strftime("%d/%m/%Y")
    if not pointer.names_replacement:
        return (
            f"\n[ĐÃ THAY THẾ] Từ ngày {when}, điều khoản này đã được thay thế bằng quy định "
            "khác không có trong ngữ cảnh. Không nêu tên hay nội dung của quy định đó."
        )
    where = pointer.citation_label or pointer.section_path or ""
    named = f"{pointer.document_title} — {where}" if pointer.document_title else where
    return (
        f"\n[ĐÃ THAY THẾ] Từ ngày {when}, điều khoản này đã được thay thế bởi: {named}. "
        "Quy định thay thế không nằm trong ngữ cảnh, không gán số trích dẫn cho nó."
    )


def assemble(
    chunks: list[RetrievedChunk], *, max_tokens: int = DEFAULT_CONTEXT_TOKENS
) -> AssembledContext:
    budget = max_tokens * CHARS_PER_TOKEN
    context = AssembledContext()
    used = 0
    for chunk in chunks:
        cost = len(chunk.text) + len(chunk.citation_label or "") + 16
        if used + cost > budget and context.items:
            # Truncating a passage would let the model cite half a clause as though it were
            # the clause. Whole passages or none.
            context.dropped += 1
            continue
        context.items.append(ContextItem(marker=len(context.items) + 1, chunk=chunk))
        used += cost
    return context


@dataclass
class VerifiedAnswer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    #: Markers the model produced that pointed at nothing, and citations whose passage did not
    #: support the sentence. Both are logged; the second is the interesting one.
    unknown_markers: list[int] = field(default_factory=list)
    unsupported_markers: list[int] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return bool(self.citations)


def verify(answer: str, context: AssembledContext) -> VerifiedAnswer:
    """Keep the citations the passages actually support; strip the rest from the text."""
    markers = [int(m) for m in _MARKER.findall(answer)]
    unknown = sorted({m for m in markers if context.by_marker(m) is None})

    supported: dict[int, ContextItem] = {}
    unsupported: set[int] = set()
    # Which claim a marker belongs to depends on where the writer put it: "… là 8% [1]." and
    # "… là 8%. [1] Ngoài ra …" are both ordinary citation style, and in the second the marker
    # sits at the head of the *next* sentence while referring to the previous one. A citation
    # counts as supported if either neighbouring claim is supported — the sentence it appears
    # in, or the one it follows.
    previous: tuple[str, set[str]] = ("", set())
    for raw_sentence in _SENTENCE.split(answer):
        markers_in = {int(m) for m in _MARKER.findall(raw_sentence)}
        sentence = _MARKER.sub(" ", raw_sentence)
        sentence_terms = _terms(sentence)
        for marker in markers_in:
            item = context.by_marker(marker)
            if item is None or marker in supported:
                continue
            here = _supports(sentence, sentence_terms, item.chunk.text)
            before = _supports(previous[0], previous[1], item.chunk.text)
            if here or before:
                supported[marker] = item
            else:
                unsupported.add(marker)
        if sentence_terms:
            previous = (sentence, sentence_terms)

    dropped = unsupported - set(supported)
    text = _strip_markers(answer, set(unknown) | dropped)

    citations = [
        Citation(
            marker=marker,
            label=item.label,
            document_id=item.chunk.document_id,
            version_id=item.chunk.version_id,
            chunk_id=item.chunk.chunk_id,
            section_path=item.chunk.section_path,
            quote=item.chunk.text.strip()[:QUOTE_CHARS],
            supersession_flag=item.chunk.supersession_flag,
            # Carried whatever the model wrote. A prompt rule improves the answer's prose; this
            # is what makes the source list correct even when the model ignored it.
            superseded_by=item.chunk.superseded_by,
        )
        for marker, item in sorted(supported.items())
    ]
    if unknown or dropped:
        log.info(
            "citations_pruned",
            extra={"unknown": unknown, "unsupported": sorted(dropped), "kept": len(citations)},
        )
    return VerifiedAnswer(
        text=text,
        citations=citations,
        unknown_markers=unknown,
        unsupported_markers=sorted(dropped),
    )


def _supports(sentence: str, sentence_terms: set[str], passage: str) -> bool:
    """Whether the passage backs the sentence citing it.

    Two conditions, and the second is the one that matters in practice: enough shared
    vocabulary to be about the same subject, and no figure the passage does not contain.
    """
    if not sentence_terms:
        return False
    invented = _numbers(sentence) - _numbers(passage)
    if invented:
        log.info("citation_number_mismatch", extra={"numbers": sorted(invented)})
        return False
    shared = sentence_terms & _terms(passage)
    if len(sentence_terms) >= 3 and len(shared) < MIN_SHARED_TERMS:
        return False
    return len(shared) / len(sentence_terms) >= SUPPORT_THRESHOLD


def _strip_markers(answer: str, markers: set[int]) -> str:
    if not markers:
        return answer
    cleaned = _MARKER.sub(lambda m: "" if int(m.group(1)) in markers else m.group(0), answer)
    # Tidy the double spaces a removed marker leaves behind, without touching line structure.
    return re.sub(r"[ \t]{2,}", " ", cleaned).replace(" .", ".").replace(" ,", ",")


def _term_weights(wanted: set[str], context: AssembledContext) -> dict[str, float]:
    """How much each question word counts, from how common it is *here*.

    "khách hàng" appears in nearly every passage a bank publishes; "kiểm quỹ" appears in one.
    Counting them equally made a question about the KYC procedure look 50% relevant to a
    withdrawal fee, because both mention customers. Weighting a term by how few of the
    retrieved passages contain it is the smallest thing that fixes that, and it needs no
    corpus statistics: the passages in hand are the sample.
    """
    documents = [_terms(item.searchable) for item in context.items] or [set()]
    weights: dict[str, float] = {}
    for term in wanted:
        frequency = sum(1 for document in documents if term in document)
        if frequency == 0:
            # A word no passage contains is evidence the corpus does not cover the question —
            # real evidence, but weaker than a rare word that *does* match is evidence for it.
            # Counting it at full weight made "ngân hàng thương mại" sink a question the
            # corpus answers, because two words of a phrasing nobody used outvoted five that
            # matched exactly.
            weights[term] = ABSENT_TERM_WEIGHT
        else:
            weights[term] = 1.0 / (1.0 + frequency)
    return weights


def best_relevance(question: str, context: AssembledContext) -> float:
    """How well the most on-topic passage matches the question.

    Measured against the passage, its citation label and its document title, with common
    words discounted (`_term_weights`).
    """
    wanted = _terms(question)
    if not wanted or context.empty:
        return 0.0
    weights = _term_weights(wanted, context)
    total = sum(weights.values()) or 1.0
    return max(
        sum(weights[term] for term in wanted & _terms(item.searchable)) / total
        for item in context.items
    )


def is_relevant(question: str, context: AssembledContext) -> bool:
    """Whether any retrieved passage is about the question at all.

    Lexical, and honestly so: this is a floor, not a relevance model. In production the
    cross-encoder reranker is what separates "about this" from "merely retrieved", and a
    question whose match is purely semantic — asked in English against Vietnamese text —
    passes that and fails this. The deterministic CI adapters have no such judgement to
    offer, which is why the answer set marks those questions `requires: semantic` rather
    than pretending they were measured.
    """
    wanted = _terms(question)
    if not wanted or context.empty:
        return False
    shared = max(len(wanted & _terms(item.searchable)) for item in context.items)
    if shared < MIN_SHARED_TERMS:
        # A one-word coincidence, in a question short enough for the ratio to be flattered.
        return False
    return best_relevance(question, context) >= RELEVANCE_RATIO
