"""The chat pipeline.

    condense → retrieve → assemble → generate → verify citations → filter → log

Every step is a place an answer can go wrong, and the order is the argument:

* **retrieve before generate, always.** The model is never asked a question without a context
  block; there is no path where it answers from what it happens to know. That is what makes
  INV-1 true for chat and not merely for search.
* **verify before filter.** Citation pruning can empty an answer, and an emptied answer must
  become a refusal rather than a fluent paragraph with nothing behind it.
* **filter before return.** The PII detector runs on the way out, on the final text, after
  everything else has finished editing it (INV-7).
* **log last, always.** Refusals, PII redactions and failures are logged exactly like
  successful answers. An audit trail that records only the answers that worked is a record of
  the times nothing went wrong (INV-11).

The service holds no ACL logic. It passes the caller's token to `retrieval-api` and receives
whatever that principal is entitled to; the filter is built there, from a verified identity,
and the `resolved_filter_id` it returns is carried into the audit record so an answer can be
tied to the exact filter that produced its evidence.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from kb_authz.principal import Principal
from kb_clients.retrieval import RetrievalClient
from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import UpstreamError
from kb_common.logging import get_logger
from kb_ports.models import GenerationPort, Message, PiiDetectorPort
from kb_schemas.api import (
    ChatRequest,
    ChatResponse,
    ExpiredMatch,
    RetrievedChunk,
    RetrieveRequest,
    RetrieveResponse,
    Source,
)
from kb_schemas.links import document_link

from kb_chat_api.condense import Condensed, QueryCondenser
from kb_chat_api.context import AssembledContext, VerifiedAnswer, assemble, is_relevant, verify
from kb_chat_api.surfaces import SurfacePolicy

log = get_logger(__name__)

#: Which channel each document reached the context by, keyed on document id (ADR-0037).
Channels = dict[uuid.UUID, str]

SUPERSESSION_WARNING = (
    "Một số điều khoản được trích dẫn đang có văn bản sửa đổi chưa được hợp nhất; "
    "hãy kiểm tra bản hợp nhất trước khi áp dụng."
)
#: Separate from the warning above, and deliberately stronger. That one says an amendment
#: exists somewhere in the document; this one says the cited clause itself has been replaced,
#: and a reader acting on the quoted figure would be acting on a rule that changed. Raised
#: from the citation rather than from the answer text, so it appears whether or not the model
#: followed the prompt rule (ADR-0033).
REPLACED_CLAUSE_WARNING = (
    "Một số điều khoản được trích dẫn đã được thay thế bởi quy định mới hơn; "
    "hãy đối chiếu quy định hiện hành trước khi áp dụng."
)
PARTIAL_CONTEXT_WARNING = (
    "Câu trả lời được xây dựng từ một phần các đoạn tìm được; hãy mở văn bản gốc nếu cần đầy đủ."
)


def _partial_coverage_warning(documents: int) -> str:
    """Say *how many* documents went unread, never which (ADR-0023/0037).

    The count is safe and the identities are not: a reader told "three more documents state
    this" learns the shape of what they are missing, while a reader told which three would
    learn the existence of documents the filter may have been keeping from them. Only budget
    truncation is spoken; the filter's own narrowing is counted into the audit record alone.
    """
    return (
        f"Còn {documents} văn bản khác cũng quy định nội dung này nhưng chưa được đưa vào "
        "câu trả lời do giới hạn độ dài; hãy mở danh sách nguồn để xem đầy đủ."
    )


@dataclass
class AnswerTrace:
    """What the audit record and the logs are built from. One answer, one trace."""

    answer_id: uuid.UUID
    surface: str
    condensed: Condensed
    actor: str = "unknown"
    on_behalf_of: str | None = None
    resolved_filter_id: str | None = None
    chunk_ids: list[str] = field(default_factory=list)
    version_ids: list[str] = field(default_factory=list)
    citations: list[int] = field(default_factory=list)
    unknown_markers: list[int] = field(default_factory=list)
    unsupported_markers: list[int] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""
    redacted_kinds: list[str] = field(default_factory=list)
    #: How each document reached the context — `ranked`, or the fact-set channel that found it
    #: (ADR-0037). Kept on the trace because it is assembly's knowledge and `_finish` is where
    #: the source list is built.
    source_channels: Channels = field(default_factory=dict)
    model: str = ""
    #: Whether the model that produced this answer sits outside the bank's network
    #: (ADR-0024). Recorded per answer, not per deployment: the routing can change under a
    #: running service, and "which answers left the building" is the question that gets asked.
    model_leaves_network: bool = False
    latency_ms: int = 0


class ChatService:
    def __init__(
        self,
        *,
        retrieval: RetrievalClient,
        generation: GenerationPort | None,
        pii: PiiDetectorPort,
        audit: AuditSink | None = None,
        condenser: QueryCondenser | None = None,
    ) -> None:
        self._retrieval = retrieval
        self._generation = generation
        self._pii = pii
        self._audit = audit
        self._condenser = condenser or QueryCondenser(generation)

    def answer(
        self,
        request: ChatRequest,
        *,
        principal: Principal,
        token: str,
        policy: SurfacePolicy,
    ) -> ChatResponse:
        started = time.monotonic()
        # INV-3/INV-4: which principals may reach a surface at all is decided before any work
        # is done, not by whether the retrieval happened to come back empty.
        policy.check_principal(principal.kind)
        # INV-7: and the public surface does not begin an answer it could not filter.
        policy.check_output_filter(self._pii)

        trace = AnswerTrace(
            answer_id=uuid.uuid4(),
            surface=policy.surface.value,
            condensed=self._condenser.condense(
                request.messages, history_turns=policy.history_turns
            ),
            # Both parties, named on every record: the bot that called and the person it
            # called for (INV-3, INV-11).
            actor=principal.audit_actor,
            on_behalf_of=principal.audit_on_behalf_of,
        )

        retrieved = self._retrieve(request, trace, token, policy)
        pool, trace.source_channels = _with_fact_members(retrieved)
        context = assemble(pool)
        if context.empty:
            # An expired instrument never reaches `context` — its chunks failed the
            # effectivity predicate — so this is the only place the difference between "the
            # bank never said anything" and "the bank said it, and it ended in December" can
            # still be told (ADR-0030).
            if retrieved.expired_matches:
                return self._refuse(
                    trace, policy, "expired", started, expired=retrieved.expired_matches
                )
            return self._refuse(trace, policy, "no context", started)
        if not is_relevant(trace.condensed.query, context):
            # Retrieval always returns its best guesses. For a question the corpus cannot
            # answer those guesses are simply the least-bad documents, and quoting one of them
            # produces a perfectly citable answer to a question nobody asked.
            return self._refuse(trace, policy, "context not relevant", started)

        answer_text, model = self._generate(trace, context, policy)
        verified = verify(answer_text, context)
        trace.model = model
        if self._generation is not None:
            trace.model_leaves_network = bool(
                self._generation.info.extra.get("leaves_network", False)
            )
        trace.citations = [citation.marker for citation in verified.citations]
        trace.unknown_markers = verified.unknown_markers
        trace.unsupported_markers = verified.unsupported_markers

        if not verified.grounded:
            # Either the model refused, or every citation it produced failed verification.
            # Both mean the same thing to the reader: we have no basis for an answer.
            return self._refuse(trace, policy, "no grounded citation", started)

        return self._finish(trace, verified, context, policy, started)

    # ------------------------------------------------------------------------ the steps

    def _retrieve(
        self,
        request: ChatRequest,
        trace: AnswerTrace,
        token: str,
        policy: SurfacePolicy,
    ) -> RetrieveResponse:
        retrieve = RetrieveRequest(
            query=trace.condensed.query,
            top_k=policy.top_k,
            facets=request.facets,
            expand_graph=policy.expand_graph,
            # What INV-13 promises an answer: not the best passage but every document that
            # states the rule (ADR-0037).
            cover_facts=True,
        )
        response = self._retrieval.retrieve(token, retrieve)
        trace.resolved_filter_id = response.resolved_filter_id
        trace.chunk_ids = [str(chunk.chunk_id) for chunk in response.chunks]
        trace.version_ids = sorted({str(chunk.version_id) for chunk in response.chunks})
        return response

    def _generate(
        self, trace: AnswerTrace, context: AssembledContext, policy: SurfacePolicy
    ) -> tuple[str, str]:
        if self._generation is None:
            raise UpstreamError("no generation backend is configured", surface=trace.surface)
        prompt = (
            policy.prompt()
            .replace("{{context}}", context.render())
            .replace("{{question}}", trace.condensed.query)
        )
        # One user message, not a system message the document text could appear to close: the
        # prompt and the context arrive as a single block whose structure the model was
        # trained against, and the instructions say plainly that context is data.
        result = self._generation.generate(
            [Message(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=policy.max_answer_tokens,
        )
        return result.text.strip(), result.model

    def _finish(
        self,
        trace: AnswerTrace,
        verified: VerifiedAnswer,
        context: AssembledContext,
        policy: SurfacePolicy,
        started: float,
    ) -> ChatResponse:
        filtered, scan = self._pii.redact(verified.text)
        trace.redacted_kinds = sorted({finding.kind for finding in scan.findings})

        warnings: list[str] = []
        if any(citation.supersession_flag for citation in verified.citations):
            warnings.append(SUPERSESSION_WARNING)
        if any(citation.superseded_by is not None for citation in verified.citations):
            warnings.append(REPLACED_CLAUSE_WARNING)
        if context.dropped_documents:
            warnings.append(_partial_coverage_warning(context.dropped_documents))
        elif context.dropped:
            # Depth lost rather than coverage: some document gave up a second clause while
            # keeping its first. Worth saying, but not as a missing source.
            warnings.append(PARTIAL_CONTEXT_WARNING)

        trace.latency_ms = int((time.monotonic() - started) * 1000)
        self._record(trace)
        return ChatResponse(
            answer=filtered,
            citations=verified.citations,
            answer_id=trace.answer_id,
            resolved_filter_id=trace.resolved_filter_id,
            warnings=warnings,
            redacted=bool(scan.findings),
            sources=_sources(context, verified, trace.source_channels),
        )

    def _refuse(
        self,
        trace: AnswerTrace,
        policy: SurfacePolicy,
        reason: str,
        started: float,
        *,
        expired: Sequence[ExpiredMatch] = (),
    ) -> ChatResponse:
        trace.refused = True
        trace.refusal_reason = reason
        trace.latency_ms = int((time.monotonic() - started) * 1000)
        self._record(trace)
        return ChatResponse(
            answer=policy.expired_refusal(expired),
            citations=[],
            answer_id=trace.answer_id,
            refused=True,
            refusal_reason=reason,
            resolved_filter_id=trace.resolved_filter_id,
        )

    # ---------------------------------------------------------------------------- audit

    def _record(self, trace: AnswerTrace) -> None:
        log.info(
            "chat_answer",
            extra={
                "answer_id": str(trace.answer_id),
                "surface": trace.surface,
                "refused": trace.refused,
                "citations": len(trace.citations),
                "chunks": len(trace.chunk_ids),
                "redacted": bool(trace.redacted_kinds),
                "latency_ms": trace.latency_ms,
            },
        )
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(
                action=AuditAction.CHAT_ANSWER,
                actor=trace.actor,
                on_behalf_of=trace.on_behalf_of,
                object_ref={
                    "answer_id": str(trace.answer_id),
                    "surface": trace.surface,
                    "chunk_ids": trace.chunk_ids,
                    "version_ids": trace.version_ids,
                },
                resolved_filter={"filter_id": trace.resolved_filter_id},
                detail={
                    # The question as searched, and whether a model rewrote it. Reconstructing
                    # an answer means reproducing the retrieval, which means knowing this.
                    "query": trace.condensed.query,
                    "condensed": trace.condensed.condensed,
                    "condense_note": trace.condensed.reason,
                    "citations": trace.citations,
                    "unknown_markers": trace.unknown_markers,
                    "unsupported_markers": trace.unsupported_markers,
                    "refused": trace.refused,
                    "refusal_reason": trace.refusal_reason,
                    # Kinds only. The audit trail names what was removed, never the value.
                    "redacted_kinds": trace.redacted_kinds,
                    "model": trace.model,
                    "model_leaves_network": trace.model_leaves_network,
                    "latency_ms": trace.latency_ms,
                },
            )
        )


def _with_fact_members(retrieved: RetrieveResponse) -> tuple[list[RetrievedChunk], Channels]:
    """The ranked passages plus every fact-set member, as one list for assembly.

    Returns the channel each document arrived by alongside them. `RetrievedChunk` has no field
    for it and should not grow one — it is an assembly fact, not a retrieval fact — but the
    label has to survive, because guessing it later from `score > 0` would call a `reference`
    member `subject`, and that is a lie in the one field whose purpose is telling a reader
    which kind of evidence they are looking at.

    Members arrive as `FactMember` and become `RetrievedChunk` here rather than in retrieval,
    because the union is an *assembly* concern: `chunks` answers "what best matches this
    question" and search pages it as it is (ADR-0037).

    `score=0.0` is deliberate and is why the conversion is worth its own function. A member was
    never ranked — it was found by an equality join or a passage-seeded vector round — so it has
    no score, and inventing one would let it sort against ranked passages as though the two
    numbers meant the same thing. Order comes from `coverage_first`, which reads document
    identity and not score.
    """
    channels: Channels = {chunk.document_id: "ranked" for chunk in retrieved.chunks}
    members = [
        RetrievedChunk(
            chunk_id=member.chunk_id,
            version_id=member.version_id,
            document_id=member.document_id,
            citation_label=member.citation_label,
            document_title=member.document_title,
            section_path=member.section_path,
            text=member.text,
            score=0.0,
            superseded_by=member.superseded_by,
        )
        for fact_set in retrieved.fact_sets
        for member in fact_set.members
    ]
    for fact_set in retrieved.fact_sets:
        for member in fact_set.members:
            channels.setdefault(member.document_id, member.channel)
    return [*retrieved.chunks, *members], channels


def _sources(
    context: AssembledContext, verified: VerifiedAnswer, channels: Channels
) -> list[Source]:
    """Every document the context drew on, with a link each (INV-13, ADR-0038).

    Built from the *context* and not from the citations, which is the whole point of having two
    lists: citations are what the model claimed, sources are what it was shown. A model that
    read four documents and cited one produces an answer that looks better-sourced than it is,
    in exactly the direction that misleads, and `cited=False` is what makes that visible.

    One entry per document, taking its first passage — the same "a fact set is about documents"
    rule assembly and coverage both follow.
    """
    marked = {citation.document_id for citation in verified.citations}
    sources: dict[uuid.UUID, Source] = {}
    for item in context.items:
        chunk = item.chunk
        if chunk.document_id in sources:
            continue
        sources[chunk.document_id] = Source(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            document_title=chunk.document_title,
            citation_label=chunk.citation_label,
            section_path=chunk.section_path,
            link=document_link(
                chunk.document_id,
                version_id=chunk.version_id,
                section_path=chunk.section_path,
            ),
            channel=channels.get(chunk.document_id, "ranked"),
            cited=chunk.document_id in marked,
            superseded_by=chunk.superseded_by,
        )
    return list(sources.values())
