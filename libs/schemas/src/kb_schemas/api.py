"""Wire contracts (plan section 6).

`RetrieveRequest.facets` may only *narrow* the server-side filter (INV-2). The request has no
field capable of expressing "show me more" — no visibility, no group list, no principal
override. That is deliberate: widening must be unrepresentable, not merely rejected.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kb_schemas.enums import RetrievalMode


class Facets(BaseModel):
    """Narrowing-only request facets."""

    model_config = ConfigDict(extra="forbid")

    #: ltree subtree, e.g. "regulations.sbv" — must be inside what the principal may see.
    category: str | None = None
    department: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    doc_class: str | None = None

    @model_validator(mode="after")
    def _check_range(self) -> Facets:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be <= date_to")
        return self


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=10, ge=1, le=50)
    mode: RetrievalMode = RetrievalMode.CURRENT
    #: Point-in-time lookup. Requires the archive scope; never the default path (INV-6).
    as_of_date: date | None = None
    facets: Facets | None = None
    expand_graph: bool = False

    @model_validator(mode="after")
    def _check_as_of(self) -> RetrieveRequest:
        if self.mode is RetrievalMode.AS_OF and self.as_of_date is None:
            raise ValueError("as_of_date is required when mode='as_of'")
        if self.mode is RetrievalMode.CURRENT and self.as_of_date is not None:
            raise ValueError("as_of_date is only valid with mode='as_of'")
        return self


class SupersededBy(BaseModel):
    """The clause that replaced this one, on a confirmed detection (ADR-0033).

    A pointer rather than a banner. It exists so an answer can say "Điều 8.2 đã được thay thế
    bởi Điều 5 Thông tư 09/2026" instead of raising a generic warning the reader cannot act on
    — the citation label and title are here for exactly that sentence.

    Its presence never means the clause was hidden. An inference is our conclusion about two
    texts, not the corpus stating that a rule ended, so it flags and points and nothing more;
    only a *declared* supersession writes the expiry ledger and takes a clause off the default
    path (ADR-0040).
    """

    model_config = ConfigDict(extra="forbid")

    #: When the replacement took effect. The one field always present: it is a fact about the
    #: clause the caller is already reading, and it names nothing.
    supersedes_from: date
    #: The replacement, when this caller may see it. **All four are None when they may not** —
    #: not because the warning is withheld, but because naming the replacing instrument would
    #: disclose the existence of a document the filter excluded, which is precisely what
    #: `compile_sql_graph` refuses to do for edges (INV-10, `[OPEN]`-3).
    #:
    #: So the warning is unconditional and the identity is not. A reader who cannot open the
    #: replacement still learns the rule changed — which is the half that stops them acting on
    #: a stale figure — and learns nothing about what replaced it.
    document_id: UUID | None = None
    section_path: str | None = None
    document_title: str | None = None
    citation_label: str | None = None

    @property
    def names_replacement(self) -> bool:
        """Whether an answer can say *what* replaced the clause, or only *that* something did."""
        return self.document_id is not None


class RetrievedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    version_id: UUID
    document_id: UUID
    citation_label: str | None = None
    #: Which document the passage is from. The citation label names the article ("Bước 3,
    #: QT 07/2024"); the title is what tells a reader — or a model — that it is the KYC
    #: procedure. Both are needed to judge whether a passage answers a question.
    document_title: str | None = None
    section_path: str | None = None
    #: Which article of the document this passage is. What the supersession warning is scoped
    #: to, and what a reference resolves against (ADR-0032/0036).
    article: int | None = None
    text: str
    score: float
    #: Matched fragments with <mark> around the terms, when the keyword engine produced them.
    #: Rendered by the search UI; the chatbot ignores them and uses `text`.
    highlights: list[str] = Field(default_factory=list)
    #: True when the source document is amended by an instrument whose consolidation is not
    #: yet approved — the answer must warn rather than present the text as current (M5).
    supersession_flag: bool = False
    #: Set when a *confirmed* clause-level supersession names what replaced this passage
    #: (M9c/M9d). Distinct from `supersession_flag` and not a finer version of it: that one says
    #: "an amendment touches this article and nobody has consolidated it yet", this one says
    #: "this exact clause was replaced, and here is by what". A chunk can carry both.
    superseded_by: SupersededBy | None = None


class ResolvedAnchor(BaseModel):
    """A cited clause, resolved to the passage it names.

    A pointer, not a quotation. `excerpt` is enough for a portal to render the reference as a
    readable link and for chat-api to decide whether the answer needs the full text — which it
    then fetches through the ordinary retrieval path, where it passes the filter again and
    becomes a real citation (ADR-0018/0036).
    """

    model_config = ConfigDict(extra="forbid")

    #: As the citing document wrote it: "12", "12.2", "12.2a".
    anchor: str
    chunk_id: UUID
    version_id: UUID
    citation_label: str | None = None
    #: An opening fragment, never the clause in full.
    excerpt: str | None = None
    #: True when the reference was detected before the target's current version was published.
    #: A statement about confidence, not a failure: this anchor was read when the target looked
    #: different, and a consolidation can renumber.
    stale: bool = False


class GraphExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    ref_type: str
    summary: str | None = None
    citation_label: str | None = None
    #: The clauses this reference names, resolved to passages under the caller's own filter.
    anchors: list[ResolvedAnchor] = Field(default_factory=list)
    #: Anchors the reference named that resolved to nothing — reported rather than dropped.
    #: The target may number its articles differently, an amendment may have inserted one we
    #: have not consolidated, or the reference may simply be wrong (ADR-0028/0036).
    unresolved_anchors: list[str] = Field(default_factory=list)


class ExpiredMatch(BaseModel):
    """A document that would have answered, had it not ceased to apply.

    Metadata only, and deliberately so: this is what turns silence into "that rule ceased on
    31/12/2026" (ADR-0030). It is never a citation and carries no text — a citation to a
    repealed rule is precisely what the expiry work exists to prevent (ADR-0018).
    """

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    document_title: str | None = None
    citation_label: str | None = None
    expired_on: date


class RetrieveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[RetrievedChunk] = Field(default_factory=list)
    expansions: list[GraphExpansion] = Field(default_factory=list)
    #: Populated only when the funnel found nothing current. The answer says the rule ended
    #: rather than that nothing was found.
    expired_matches: list[ExpiredMatch] = Field(default_factory=list)
    #: Hash of the filter actually applied — joins this response to its audit record (INV-11).
    resolved_filter_id: str


class ResolveAnchorRequest(BaseModel):
    """Resolve a reference the platform parsed itself.

    Distinct from `CitationLookupRequest`, which trigram-matches text a *human* typed. Here the
    document is already identified and the anchor is already structured, so resolution is an
    equality join with no threshold to tune.
    """

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    anchors: list[str] = Field(min_length=1, max_length=32)
    as_of_date: date | None = None


class ResolveAnchorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: list[ResolvedAnchor] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    resolved_filter_id: str


class CitationLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation: str = Field(min_length=1, max_length=500)
    as_of_date: date | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Only the two roles a caller may assert. A caller-supplied `system` message would be a
    #: prompt-injection channel with a permission slip.
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    conversation_id: UUID | None = None
    #: Narrowing only, exactly as in `RetrieveRequest` — the chat surface cannot widen what
    #: the funnel would return (INV-2).
    facets: Facets | None = None

    @model_validator(mode="after")
    def _ends_with_a_question(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The `[n]` marker in the answer text this citation resolves.
    marker: int
    label: str
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    section_path: str | None = None
    #: The passage the answer was drawn from, so a reader can check the claim without
    #: opening the document. Trimmed, never paraphrased.
    quote: str | None = None
    #: The cited document is amended and not yet consolidated (M5). Rendered as a warning.
    supersession_flag: bool = False


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    answer_id: UUID
    refused: bool = False
    refusal_reason: str | None = None
    #: Joins the answer to its audit record and to the filter that produced its context
    #: (INV-11). Present even on a refusal: a refusal is also an answer to account for.
    resolved_filter_id: str | None = None
    #: Surfaced to the reader — supersession, an incomplete context, a partial answer.
    warnings: list[str] = Field(default_factory=list)
    #: True when the PII output filter removed something before the answer was returned
    #: (INV-7). The audit record says what kind; the response never repeats the value.
    redacted: bool = False
