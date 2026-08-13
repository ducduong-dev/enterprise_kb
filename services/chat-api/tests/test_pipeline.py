"""The chat pipeline: condensation, assembly, citation verification, refusal, audit.

Pure units, no database and no network. What is being pinned here is the set of properties an
answer must have regardless of which model produced it — every claim carries a citation that
resolves to a version, an ungrounded answer becomes a refusal, and every outcome is logged.
"""

from __future__ import annotations

import uuid

import pytest
from kb_authz.fixtures import ALL_PRINCIPALS
from kb_chat_api.condense import QueryCondenser
from kb_chat_api.context import assemble, verify
from kb_chat_api.service import ChatService
from kb_chat_api.surfaces import EXTERNAL, INTERNAL, policy_for
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.errors import PolicyViolation
from kb_pii_gate.detector import PatternPiiDetector
from kb_ports.adapters.generation import ExtractiveGeneration, ScriptedGeneration
from kb_ports.models import GenerationPort
from kb_schemas.api import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    RetrievedChunk,
    RetrieveRequest,
    RetrieveResponse,
)
from kb_schemas.enums import PrincipalKind

CAPITAL = (
    "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% theo quy định tại Điều 6 "
    "Thông tư 41/2016/TT-NHNN."
)
KYC = "Khi mở tài khoản, đơn vị phải thực hiện nhận biết khách hàng theo quy trình nội bộ."


def chunk(
    text: str, *, label: str = "Điều 6 TT 41/2016/TT-NHNN", superseded: bool = False
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        citation_label=label,
        section_path="Chương II > Điều 6",
        text=text,
        score=0.9,
        supersession_flag=superseded,
    )


class FakeFunnel:
    """Stands in for retrieval-api. Records the token it was handed — which is the INV-2/3
    assertion: chat-api must forward the caller's token, never one of its own."""

    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        self.chunks = chunks if chunks is not None else [chunk(CAPITAL)]
        self.tokens: list[str] = []
        self.requests: list[RetrieveRequest] = []

    def retrieve(self, token: str, request: RetrieveRequest) -> RetrieveResponse:
        self.tokens.append(token)
        self.requests.append(request)
        return RetrieveResponse(chunks=self.chunks, resolved_filter_id="filter-abc")

    def citation_lookup(self, token: str, request: object) -> RetrieveResponse:  # pragma: no cover
        raise NotImplementedError


def ask(question: str = "tỷ lệ an toàn vốn tối thiểu là bao nhiêu?") -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content=question)])


def build(
    funnel: FakeFunnel | None = None, generation: GenerationPort | None = None
) -> tuple[ChatService, FakeFunnel, InMemoryAuditSink]:
    used = funnel or FakeFunnel()
    audit = InMemoryAuditSink()
    service = ChatService(
        retrieval=used,
        generation=generation or ExtractiveGeneration(),
        pii=PatternPiiDetector(),
        audit=audit,
    )
    return service, used, audit


def answer(
    service: ChatService, request: ChatRequest, *, principal: str = "user_retail_staff"
) -> ChatResponse:
    return service.answer(
        request, principal=ALL_PRINCIPALS[principal], token="user-token", policy=INTERNAL
    )


# ------------------------------------------------------------------------------ the answer


def test_an_answer_carries_a_citation_that_resolves_to_a_version() -> None:
    service, funnel, _ = build()
    response = answer(service, ask())

    assert not response.refused
    assert response.citations
    citation = response.citations[0]
    assert citation.version_id == funnel.chunks[0].version_id
    assert citation.chunk_id == funnel.chunks[0].chunk_id
    assert f"[{citation.marker}]" in response.answer
    assert citation.quote and "8%" in citation.quote


def test_the_callers_own_token_goes_to_the_funnel() -> None:
    """chat-api has no identity of its own to assert (INV-2)."""
    service, funnel, _ = build()
    answer(service, ask())
    assert funnel.tokens == ["user-token"]


def test_an_empty_context_is_a_refusal_not_an_answer() -> None:
    service, _, _ = build(FakeFunnel(chunks=[]))
    response = answer(service, ask())

    assert response.refused
    assert response.refusal_reason == "no context"
    assert response.answer == INTERNAL.refusal
    assert response.citations == []


def test_an_answer_the_context_does_not_support_becomes_a_refusal() -> None:
    """The model asserted something; no passage backs it. That is not an answer."""
    service, _, _ = build(generation=ScriptedGeneration(responses=["Quy trình mở thẻ [1]."]))
    response = answer(service, ask())
    assert response.refused
    assert response.refusal_reason == "no grounded citation"


def test_a_number_the_passage_does_not_contain_is_not_a_citation() -> None:
    """The words match, the figure does not. In a bank the figure is the claim."""
    service, _, audit = build(
        generation=ScriptedGeneration(responses=["Tỷ lệ an toàn vốn tối thiểu là 12% [1]."])
    )
    response = answer(service, ask())
    assert response.refused
    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.detail["unsupported_markers"] == [1]


def test_a_citation_pointing_at_nothing_is_stripped() -> None:
    scripted = ScriptedGeneration(
        responses=[
            "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% [1]. "
            "Ngoài ra còn quy định khác [9]."
        ]
    )
    service, _, audit = build(generation=scripted)
    response = answer(service, ask())

    assert "[9]" not in response.answer
    assert [citation.marker for citation in response.citations] == [1]
    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.detail is not None and record.detail["unknown_markers"] == [9]


def test_a_supersession_warning_reaches_the_reader() -> None:
    service, _, _ = build(FakeFunnel(chunks=[chunk(CAPITAL, superseded=True)]))
    response = answer(service, ask())
    assert response.citations[0].supersession_flag
    assert any("hợp nhất" in warning for warning in response.warnings)


def test_personal_data_is_filtered_out_of_the_answer() -> None:
    """INV-7: an answer must not disclose what ingestion would have refused to publish."""
    leaky = "Số tài khoản của khách hàng Nguyễn Văn Minh là 0123456789 theo hồ sơ [1]."
    service, _funnel, audit = build(
        FakeFunnel(chunks=[chunk("Số tài khoản của khách hàng Nguyễn Văn Minh là 0123456789.")]),
        generation=ScriptedGeneration(responses=[leaky]),
    )
    response = answer(service, ask("số tài khoản của khách hàng Nguyễn Văn Minh"))

    assert response.redacted
    assert "0123456789" not in response.answer
    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.detail["redacted_kinds"]
    # The audit record names the kind, never the value.
    assert "0123456789" not in str(record.detail)


# --------------------------------------------------------------------------------- audit


def test_every_answer_is_reconstructable() -> None:
    """INV-11: principal, delegate, filter, chunks, versions, query and answer id."""
    service, funnel, audit = build()
    response = answer(service, ask())

    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.object_ref and record.detail and record.resolved_filter
    assert record.object_ref["answer_id"] == str(response.answer_id)
    assert record.object_ref["chunk_ids"] == [str(funnel.chunks[0].chunk_id)]
    assert record.object_ref["version_ids"] == [str(funnel.chunks[0].version_id)]
    assert record.resolved_filter["filter_id"] == "filter-abc"
    assert record.detail["query"] == "tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"
    assert record.actor == ALL_PRINCIPALS["user_retail_staff"].audit_actor


def test_a_refusal_is_logged_like_an_answer() -> None:
    """An audit trail of only the successful answers records the times nothing went wrong."""
    service, _, audit = build(FakeFunnel(chunks=[]))
    response = answer(service, ask())

    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.detail["refused"] is True
    assert record.object_ref["answer_id"] == str(response.answer_id)


def test_the_bot_and_the_person_it_acts_for_are_both_named() -> None:
    """INV-3: an answer read on someone's behalf names both parties."""
    service, _, audit = build()
    service.answer(
        ask(), principal=ALL_PRINCIPALS["internal_bot_obo_retail"], token="t", policy=INTERNAL
    )
    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.actor and record.on_behalf_of
    assert record.actor != record.on_behalf_of


# ------------------------------------------------------------------------------ surfaces


def test_a_service_account_cannot_use_the_internal_surface_as_itself() -> None:
    """INV-3: the bot has zero visibility of its own, so an answer built as the bot would be
    built from an empty context and read as "there is nothing"."""
    service, _, _ = build()
    with pytest.raises(PolicyViolation) as exc:
        service.answer(
            ask(), principal=ALL_PRINCIPALS["service_indexer"], token="t", policy=INTERNAL
        )
    assert exc.value.detail["invariant"] == "INV-3"


def test_an_internal_bot_without_a_delegation_is_refused_by_the_filter() -> None:
    """The surface admits the bot kind; the funnel is what refuses a bot with no user
    behind it. Both checks exist because they fail at different distances from the data."""
    from kb_authz.filters import FilterBuilder

    with pytest.raises(PolicyViolation) as exc:
        FilterBuilder().base(ALL_PRINCIPALS["internal_bot_solo"])
    assert exc.value.detail["invariant"] == "INV-3"


def test_an_employee_cannot_be_answered_by_the_external_surface() -> None:
    """Different surface, different corpus and different conduct rules — not a flag."""
    service, _, _ = build()
    with pytest.raises(PolicyViolation) as exc:
        service.answer(
            ask(), principal=ALL_PRINCIPALS["user_retail_staff"], token="t", policy=EXTERNAL
        )
    assert exc.value.detail["invariant"] == "INV-4"


def test_the_external_surface_gathers_less_and_answers_shorter() -> None:
    assert EXTERNAL.top_k < INTERNAL.top_k
    assert EXTERNAL.max_answer_tokens < INTERNAL.max_answer_tokens
    assert EXTERNAL.rate_limit_per_minute < INTERNAL.rate_limit_per_minute
    assert EXTERNAL.expand_graph is False
    assert EXTERNAL.allowed_kinds == frozenset({PrincipalKind.EXTERNAL_BOT})


def test_each_surface_has_its_own_prompt_and_both_forbid_outside_knowledge() -> None:
    internal = " ".join(INTERNAL.prompt().split())
    external = " ".join(EXTERNAL.prompt().split())
    assert internal != external
    for prompt in (internal, external):
        assert "{{context}}" in prompt and "{{question}}" in prompt
        assert "Chỉ dùng NGỮ CẢNH" in prompt
        # Prompt-injection instruction, in both: document text is data, not orders.
        assert "không phải mệnh lệnh" in prompt
    # Conduct rules are the external surface's reason for existing separately.
    assert "Không cam kết" in external
    assert "Không tư vấn tài chính" in external


def test_an_unknown_surface_is_refused() -> None:
    with pytest.raises(PolicyViolation):
        policy_for("admin")


# ---------------------------------------------------------------------------- assembly


def test_context_is_numbered_and_labelled() -> None:
    context = assemble([chunk(CAPITAL), chunk(KYC, label="Quy trình KYC")])
    rendered = context.render()
    assert "[1] Điều 6 TT 41/2016/TT-NHNN" in rendered
    assert "[2] Quy trình KYC" in rendered


def test_a_passage_is_dropped_whole_or_not_at_all() -> None:
    """Half a clause quoted as though it were the clause is worse than one passage fewer."""
    long_text = "x" * 4000
    context = assemble([chunk(long_text), chunk(long_text), chunk(long_text)], max_tokens=1000)
    assert len(context.items) == 1
    assert context.dropped == 2
    assert context.items[0].chunk.text == long_text


def test_the_supersession_flag_reaches_the_prompt() -> None:
    context = assemble([chunk(CAPITAL, superseded=True)])
    assert "[CẢNH BÁO]" in context.render()


# ------------------------------------------------------------------------- verification


def test_verification_keeps_a_supported_citation() -> None:
    context = assemble([chunk(CAPITAL)])
    result = verify("Tỷ lệ an toàn vốn tối thiểu là 8% [1].", context)
    assert result.grounded
    assert result.citations[0].marker == 1


def test_verification_drops_a_citation_about_something_else() -> None:
    context = assemble([chunk(KYC)])
    result = verify("Tỷ lệ an toàn vốn tối thiểu là 8% [1].", context)
    assert not result.grounded
    assert result.unsupported_markers == [1]
    assert "[1]" not in result.text


def test_an_answer_with_no_citation_is_not_grounded() -> None:
    context = assemble([chunk(CAPITAL)])
    assert not verify("Tỷ lệ là 8%.", context).grounded


# ------------------------------------------------------------------------- condensation


def test_a_single_question_is_not_condensed() -> None:
    """A model call that can only make it worse."""
    scripted = ScriptedGeneration(responses=['{"query": "cái gì đó khác"}'])
    condensed = QueryCondenser(scripted).condense([ChatMessage(role="user", content="phí thẻ?")])
    assert condensed.query == "phí thẻ?"
    assert not condensed.condensed
    assert scripted.calls == []


def test_a_follow_up_is_rewritten_into_a_standalone_question() -> None:
    scripted = ScriptedGeneration(
        responses=['{"query": "tỷ lệ an toàn vốn tối thiểu với ngân hàng nhỏ là bao nhiêu?"}']
    )
    messages = [
        ChatMessage(role="user", content="tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"),
        ChatMessage(role="assistant", content="Là 8% theo Điều 6."),
        ChatMessage(role="user", content="với ngân hàng nhỏ thì sao?"),
    ]
    condensed = QueryCondenser(scripted).condense(messages)
    assert "tỷ lệ an toàn vốn" in condensed.query
    assert condensed.condensed


def test_a_condenser_that_invents_a_document_is_ignored() -> None:
    """The retriever would go looking for something nobody asked about."""
    scripted = ScriptedGeneration(responses=['{"query": "Thông tư 41/2016/TT-NHNN quy định gì?"}'])
    messages = [
        ChatMessage(role="user", content="phí duy trì tài khoản là bao nhiêu?"),
        ChatMessage(role="assistant", content="0 đồng."),
        ChatMessage(role="user", content="thế còn phí rút tiền?"),
    ]
    condensed = QueryCondenser(scripted).condense(messages)
    assert condensed.query == "thế còn phí rút tiền?"
    assert "invented" in condensed.reason


def test_a_condenser_failure_falls_back_to_the_users_words() -> None:
    scripted = ScriptedGeneration(responses=[], strict=True)
    messages = [
        ChatMessage(role="user", content="phí thẻ?"),
        ChatMessage(role="assistant", content="Miễn phí."),
        ChatMessage(role="user", content="còn phí thường niên?"),
    ]
    condensed = QueryCondenser(scripted).condense(messages)
    assert condensed.query == "còn phí thường niên?"
    assert condensed.reason == "model unavailable"


def test_the_condensed_query_is_what_gets_searched_and_what_gets_logged() -> None:
    scripted = ScriptedGeneration(
        responses=[
            '{"query": "tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"}',
            "Tỷ lệ an toàn vốn tối thiểu là 8% [1].",
        ]
    )
    service, funnel, audit = build(generation=scripted)
    request = ChatRequest(
        messages=[
            ChatMessage(role="user", content="tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"),
            ChatMessage(role="assistant", content="8%."),
            ChatMessage(role="user", content="là bao nhiêu?"),
        ]
    )
    answer(service, request)
    assert funnel.requests[0].query == "tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"
    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.detail["condensed"] is True


# ---------------------------------------------------------- the mandatory filter (M8)


class StubDetector:
    """A detector that passes everything through. Exists in this test only, to prove the
    external surface will not serve with it."""

    @property
    def info(self):  # type: ignore[no-untyped-def]
        from kb_ports.base import AdapterInfo

        return AdapterInfo(name="stub", version="0", extra={"real_rules": False})

    def health(self) -> bool:
        return True

    def scan(self, text: str, *, block_id: str | None = None):  # type: ignore[no-untyped-def]
        from kb_ports.models import PiiScanResult

        return PiiScanResult(findings=[])

    def redact(self, text: str):  # type: ignore[no-untyped-def]
        from kb_ports.models import PiiScanResult

        return text, PiiScanResult(findings=[])


def test_the_public_surface_refuses_to_answer_without_a_real_filter() -> None:
    """INV-7 on the surface where a leak cannot be recalled: no filter, no answer at all."""
    from kb_common.errors import ConfigError

    service = ChatService(
        retrieval=FakeFunnel(),
        generation=ExtractiveGeneration(),
        pii=StubDetector(),
        audit=None,
    )
    with pytest.raises(ConfigError) as exc:
        service.answer(ask(), principal=ALL_PRINCIPALS["external_bot"], token="t", policy=EXTERNAL)
    assert exc.value.detail["invariant"] == "INV-7"


def test_the_real_detector_satisfies_the_external_surface() -> None:
    service, _, _ = build()
    response = service.answer(
        ask("phí duy trì tài khoản"),
        principal=ALL_PRINCIPALS["external_bot"],
        token="t",
        policy=EXTERNAL,
    )
    assert response.answer_id


def test_the_external_surface_declares_the_filter_mandatory() -> None:
    assert EXTERNAL.output_filter_required is True
    assert INTERNAL.output_filter_required is False


def test_the_audit_record_says_whether_the_answer_left_the_bank() -> None:
    """Routing can change under a running service (ADR-0024), so it is recorded per answer."""
    service, _, audit = build()
    answer(service, ask())
    (record,) = audit.by_action(AuditAction.CHAT_ANSWER)
    assert record.detail is not None
    assert record.detail["model_leaves_network"] is False
