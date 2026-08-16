"""The chat endpoint: who may ask, on whose behalf, and how often.

The pipeline is tested in `test_pipeline.py`. This file is about the edge — specifically the
delegation step, which is where INV-3 either holds or does not: a bot must exchange its own
token for the end user's before anything is retrieved, and the token that reaches the funnel
must be the exchanged one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from kb_authz.exchange import StaticTokenExchanger
from kb_chat_api.main import (
    ON_BEHALF_OF_HEADER,
    RateLimiter,
    app,
    chat_service,
    exchanger,
    limiter,
    retrieval,
)
from kb_chat_api.service import ChatService
from kb_chat_api.surfaces import policy_for
from kb_common.audit import InMemoryAuditSink
from kb_common.config import get_settings, reset_settings_cache
from kb_pii_gate.detector import PatternPiiDetector
from kb_ports.adapters.generation import ExtractiveGeneration
from kb_schemas.api import ExpiredMatch, RetrievedChunk, RetrieveRequest, RetrieveResponse

SECRET = "kb-test-secret"
CAPITAL = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% theo Điều 6."
QUESTION = {"messages": [{"role": "user", "content": "tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"}]}


def token(
    subject: str,
    *,
    groups: list[str] | None = None,
    username: str | None = None,
    azp: str | None = None,
    act: dict[str, str] | None = None,
) -> str:
    settings = get_settings().oidc
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "groups": groups if groups is not None else ["/dept/retail"],
        "realm_access": {"roles": []},
    }
    if username is not None:
        claims["preferred_username"] = username
    if azp is not None:
        claims["azp"] = azp
    if act is not None:
        claims["act"] = act
    return jwt.encode(claims, SECRET, algorithm="HS256")


def user_token() -> str:
    return token("u-retail-staff", username="u-retail-staff")


def bot_service_token() -> str:
    """The internal bot's own account: a service principal with no document visibility."""
    return token(
        "service-account-kb-internal-bot",
        username="service-account-kb-internal-bot",
        azp="kb-internal-bot",
        groups=[],
    )


def exchanged_token(user_subject: str = "u-retail-staff") -> str:
    """What Keycloak returns: the user's subject, with the bot recorded as the actor."""
    return token(
        user_subject,
        username=user_subject,
        act={"sub": "service-account-kb-internal-bot", "client_id": "kb-internal-bot"},
    )


def external_token() -> str:
    return token("service-account-kb-external-bot", azp="kb-external-bot", groups=[])


class FakeFunnel:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def retrieve(self, token: str, request: RetrieveRequest) -> RetrieveResponse:
        self.tokens.append(token)
        return RetrieveResponse(
            chunks=[
                RetrievedChunk(
                    chunk_id=uuid.uuid4(),
                    version_id=uuid.uuid4(),
                    document_id=uuid.uuid4(),
                    citation_label="Điều 6 TT 41/2016/TT-NHNN",
                    text=CAPITAL,
                    score=0.9,
                )
            ],
            resolved_filter_id="filter-abc",
        )

    def citation_lookup(self, token: str, request: object) -> RetrieveResponse:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
def insecure_tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KB_ENV", "test")
    monkeypatch.setenv("KB_OIDC_ALLOW_INSECURE_TOKENS", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def funnel() -> FakeFunnel:
    return FakeFunnel()


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def exchange() -> StaticTokenExchanger:
    return StaticTokenExchanger(
        tokens={"u-retail-staff": exchanged_token()},
        refuse=frozenset({"u-not-allowed"}),
    )


@pytest.fixture
def client(
    funnel: FakeFunnel, audit: InMemoryAuditSink, exchange: StaticTokenExchanger
) -> Iterator[TestClient]:
    # One limiter for the whole test, so a budget actually accumulates across requests.
    rate = RateLimiter()
    app.dependency_overrides[retrieval] = lambda: funnel
    app.dependency_overrides[exchanger] = lambda: exchange
    app.dependency_overrides[limiter] = lambda: rate
    app.dependency_overrides[chat_service] = lambda: ChatService(
        retrieval=funnel,
        generation=ExtractiveGeneration(),
        pii=PatternPiiDetector(),
        audit=audit,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def ask(client: TestClient, *, bearer: str, surface: str = "internal", **headers: str) -> Response:
    response: Response = client.post(
        f"/v1/chat/{surface}",
        json=QUESTION,
        headers={"Authorization": f"Bearer {bearer}", **headers},
    )
    return response


# ------------------------------------------------------------------------ authentication


def test_an_anonymous_question_is_refused(client: TestClient) -> None:
    assert client.post("/v1/chat/internal", json=QUESTION).status_code == 401


def test_a_forged_token_is_refused(client: TestClient) -> None:
    forged = jwt.encode({"sub": "attacker"}, "wrong-secret", algorithm="HS256")
    assert ask(client, bearer=forged).status_code == 401


def test_an_employee_gets_an_answer_with_citations(client: TestClient) -> None:
    response = ask(client, bearer=user_token())
    assert response.status_code == 200
    body = response.json()
    assert body["citations"]
    assert body["answer_id"]
    assert body["resolved_filter_id"] == "filter-abc"


# --------------------------------------------------------------------------- delegation


def test_the_bot_must_name_the_person_it_is_asking_for(client: TestClient) -> None:
    """INV-3: the bot's own account can see nothing, so answering as itself would answer
    "there is nothing" to every question."""
    response = ask(client, bearer=bot_service_token())
    assert response.status_code == 403
    assert response.json()["error"] == "policy_violation"


def test_the_exchanged_token_is_what_reaches_the_funnel(
    client: TestClient, funnel: FakeFunnel, exchange: StaticTokenExchanger
) -> None:
    service_token = bot_service_token()
    response = ask(
        client,
        bearer=service_token,
        **{ON_BEHALF_OF_HEADER: "u-retail-staff"},
    )
    assert response.status_code == 200
    assert exchange.calls == [(service_token, "u-retail-staff")]
    # The bot's own token never reaches retrieval.
    assert funnel.tokens == [exchange.tokens["u-retail-staff"]]
    assert service_token not in funnel.tokens


def test_an_answer_on_someones_behalf_names_both_parties(
    client: TestClient, audit: InMemoryAuditSink
) -> None:
    ask(client, bearer=bot_service_token(), **{ON_BEHALF_OF_HEADER: "u-retail-staff"})
    (record,) = audit.records
    assert record.actor == "service-account-kb-internal-bot"
    assert record.on_behalf_of == "u-retail-staff"


def test_a_refused_exchange_fails_the_request(client: TestClient) -> None:
    """Keycloak says the bot may not act for this person. There is no fallback."""
    response = ask(client, bearer=bot_service_token(), **{ON_BEHALF_OF_HEADER: "u-not-allowed"})
    assert response.status_code == 403


def test_an_exchange_without_an_actor_claim_is_refused(
    client: TestClient, exchange: StaticTokenExchanger
) -> None:
    """A token with no `act` records no delegation — the reads would be attributed to nobody."""
    exchange.tokens["u-retail-staff"] = token("u-retail-staff", username="u-retail-staff")
    response = ask(client, bearer=bot_service_token(), **{ON_BEHALF_OF_HEADER: "u-retail-staff"})
    assert response.status_code == 403


# ----------------------------------------------------------------------------- surfaces


def test_an_employee_may_not_use_the_public_surface(client: TestClient) -> None:
    response = ask(client, bearer=user_token(), surface="external")
    assert response.status_code == 403


def test_the_external_bot_may_not_use_the_internal_surface(client: TestClient) -> None:
    response = ask(client, bearer=external_token(), surface="internal")
    assert response.status_code == 403


def test_an_unknown_surface_is_not_routed(client: TestClient) -> None:
    assert ask(client, bearer=user_token(), surface="admin").status_code == 422


def test_the_policy_endpoint_reports_what_the_surface_will_do(client: TestClient) -> None:
    response = client.get(
        "/v1/chat/external/policy", headers={"Authorization": f"Bearer {external_token()}"}
    )
    assert response.status_code == 200
    assert response.json()["rate_limit_per_minute"] == 10
    assert response.json()["expand_graph"] is False


# --------------------------------------------------------------------------- rate limit


def test_a_caller_that_loops_is_slowed_down(client: TestClient) -> None:
    bearer = external_token()
    codes = [ask(client, bearer=bearer, surface="external").status_code for _ in range(12)]
    assert codes.count(200) == 10  # the external surface's budget
    assert codes[-1] == 429


def test_the_budget_is_per_principal(client: TestClient) -> None:
    """One noisy caller must not silence everyone else."""
    for _ in range(10):
        ask(client, bearer=external_token(), surface="external")
    assert ask(client, bearer=user_token()).status_code == 200


# ------------------------------------------------------------------ deployment surface (M8)


def test_a_dmz_deployment_refuses_the_internal_surface(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`KB_SURFACE=dmz` is what the DMZ deployment sets, and it must bind the *process*.

    The gateway keeps the internal path off the public edge; this keeps it off the public
    process, so reaching the container directly — a stray port, a misrouted rule, a neighbour
    with a shell — still cannot ask the internal bot anything (INV-4).
    """
    monkeypatch.setenv("KB_SURFACE", "dmz")
    reset_settings_cache()

    refused = ask(client, bearer=user_token(), surface="internal")
    assert refused.status_code == 403
    assert refused.json()["error"] == "policy_violation"

    # And the public surface still works on that deployment.
    allowed = ask(client, bearer=external_token(), surface="external")
    assert allowed.status_code == 200


def test_an_internal_deployment_still_serves_both(client: TestClient) -> None:
    """The default deployment is the internal one; nothing about M8 narrows it."""
    assert ask(client, bearer=user_token(), surface="internal").status_code == 200
    assert ask(client, bearer=external_token(), surface="external").status_code == 200


# ------------------------------------------------------------- expired refusals (M9a)


def _expired(title: str = "Biểu phí dịch vụ 2023") -> ExpiredMatch:
    return ExpiredMatch(
        document_id=uuid.uuid4(),
        document_title=title,
        citation_label="Mục 2, BP 01/2023",
        expired_on=date(2026, 12, 31),
    )


def test_the_internal_surface_names_what_ceased_instead_of_saying_nothing() -> None:
    """Silence is indistinguishable from "the bank never said anything about this". Naming the
    instrument and the date is the whole point of M9a's refusal (ADR-0028/0030)."""
    answer = policy_for("internal").expired_refusal([_expired()])

    assert "Biểu phí dịch vụ 2023" in answer
    assert "31/12/2026" in answer
    assert "hết hiệu lực" in answer


def test_the_public_surface_does_not_disclose_that_the_document_exists() -> None:
    """`[OPEN]`-10: naming a repealed fee schedule to a customer is more useful *and* is an
    existence disclosure. Until Legal rules, the generic refusal stands."""
    generic = policy_for("external").refusal
    assert policy_for("external").expired_refusal([_expired()]) == generic


def test_a_refusal_with_nothing_expired_is_the_ordinary_one() -> None:
    assert policy_for("internal").expired_refusal([]) == policy_for("internal").refusal


def test_the_named_refusal_never_quotes_the_repealed_text() -> None:
    """A citation to a rule that no longer applies is exactly what the milestone prevents, and
    an apology that paraphrases the rule is still a citation (ADR-0018). `ExpiredMatch` carries
    no text at all, which is what makes that structural rather than a prompt instruction."""
    assert "text" not in ExpiredMatch.model_fields
