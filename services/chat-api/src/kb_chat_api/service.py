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
from dataclasses import dataclass, field

from kb_authz.principal import Principal
from kb_clients.retrieval import RetrievalClient
from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import UpstreamError
from kb_common.logging import get_logger
from kb_ports.models import GenerationPort, Message, PiiDetectorPort
from kb_schemas.api import ChatRequest, ChatResponse, RetrieveRequest, RetrieveResponse

from kb_chat_api.condense import Condensed, QueryCondenser
from kb_chat_api.context import AssembledContext, VerifiedAnswer, assemble, is_relevant, verify
from kb_chat_api.surfaces import SurfacePolicy

log = get_logger(__name__)

SUPERSESSION_WARNING = (
    "Một số điều khoản được trích dẫn đang có văn bản sửa đổi chưa được hợp nhất; "
    "hãy kiểm tra bản hợp nhất trước khi áp dụng."
)
PARTIAL_CONTEXT_WARNING = (
    "Câu trả lời được xây dựng từ một phần các đoạn tìm được; hãy mở văn bản gốc nếu cần đầy đủ."
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
        context = assemble(retrieved.chunks)
        if context.empty:
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
        if context.dropped:
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
        )

    def _refuse(
        self, trace: AnswerTrace, policy: SurfacePolicy, reason: str, started: float
    ) -> ChatResponse:
        trace.refused = True
        trace.refusal_reason = reason
        trace.latency_ms = int((time.monotonic() - started) * 1000)
        self._record(trace)
        return ChatResponse(
            answer=policy.refusal,
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
