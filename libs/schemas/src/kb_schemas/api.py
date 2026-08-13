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
    text: str
    score: float
    #: Matched fragments with <mark> around the terms, when the keyword engine produced them.
    #: Rendered by the search UI; the chatbot ignores them and uses `text`.
    highlights: list[str] = Field(default_factory=list)
    #: True when the source document is amended by an instrument whose consolidation is not
    #: yet approved — the answer must warn rather than present the text as current (M5).
    supersession_flag: bool = False


class GraphExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    ref_type: str
    summary: str | None = None
    citation_label: str | None = None


class RetrieveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[RetrievedChunk] = Field(default_factory=list)
    expansions: list[GraphExpansion] = Field(default_factory=list)
    #: Hash of the filter actually applied — joins this response to its audit record (INV-11).
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
