"""ASGI entrypoint for kb-chat-api.

Both chat surfaces, one pipeline (`service.ChatService`), one funnel (`kb_clients.retrieval`).
This module is the edge: it decides *who is asking*, obtains the right token to ask on their
behalf, applies the rate limit, and hands off. It contains no ACL logic and no prompt.

The delegation step is the part worth reading. An internal bot arrives with its own service
token, which grants it no document visibility whatsoever (INV-3). Before anything is retrieved
it must exchange that token for one issued for the end user it names — and if Keycloak refuses
the exchange, the request fails. It never falls back to answering as the bot, because the bot
can see nothing, and "no results" is indistinguishable to a user from "nothing exists".
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Depends, FastAPI, Header, Path
from kb_authz.exchange import KeycloakTokenExchanger, TokenExchanger
from kb_authz.principal import Principal
from kb_authz.tokens import (
    OidcTokenVerifier,
    SharedSecretVerifier,
    TokenVerifier,
    resolve_principal,
)
from kb_clients.retrieval import HttpRetrievalClient, RetrievalClient
from kb_common.app import create_app
from kb_common.audit import SqlAuditSink
from kb_common.config import get_settings
from kb_common.db import get_session
from kb_common.errors import AuthenticationError, PolicyViolation, RateLimited
from kb_common.logging import get_logger
from kb_pii_gate.detector import PatternPiiDetector
from kb_ports.models import GenerationPort, PiiDetectorPort
from kb_schemas.api import ChatRequest, ChatResponse
from kb_schemas.enums import PrincipalKind
from sqlalchemy.orm import Session

from kb_chat_api.service import ChatService
from kb_chat_api.surfaces import SurfacePolicy, policy_for

log = get_logger(__name__)

app: FastAPI = create_app("kb-chat-api")

#: Header a service account uses to name the person it is acting for. The value is a subject,
#: not a group list or a filter: nothing a caller sends can widen what that person may see.
ON_BEHALF_OF_HEADER = "X-KB-On-Behalf-Of"


def check_deployment_surface(surface: str) -> None:
    """A DMZ process answers the public surface and nothing else (INV-4).

    Two vocabularies meet here, each right in its own place: `KB_SURFACE` describes the
    *deployment* (`internal` or `dmz`), and the path parameter describes the *request*
    (`internal` or `external`). A `dmz` deployment serves external requests only.

    This is what makes the compose profile mean something: routing keeps the internal path off
    the public gateway, and this keeps it off the public *process*, so reaching the container
    directly — a misconfigured route, a port left open, a container escape on a neighbour —
    still cannot ask the internal bot a question. The two controls fail independently, which
    is the point of having both.
    """
    deployment = get_settings().surface
    if deployment == "dmz" and surface != "external":
        raise PolicyViolation(
            "this deployment serves the external surface only",
            surface=surface,
            deployment_surface=deployment,
            invariant="INV-4",
        )


def verifier() -> TokenVerifier:
    settings = get_settings()
    if settings.oidc.allow_insecure_tokens and not settings.is_production:
        return SharedSecretVerifier()
    return OidcTokenVerifier(settings.oidc)


def exchanger() -> TokenExchanger:
    return KeycloakTokenExchanger()


def retrieval() -> RetrievalClient:
    return HttpRetrievalClient()


def generation() -> GenerationPort:
    settings = get_settings()
    if settings.use_deterministic_models:
        # Deterministic quoting adapter: the pipeline's guarantees are testable, the model's
        # fluency is not. It declares `real_model: False` (ADR-0019).
        from kb_ports.adapters.generation import ExtractiveGeneration

        return ExtractiveGeneration()
    from kb_ports.adapters.generation import OpenAiCompatibleGeneration

    return OpenAiCompatibleGeneration(
        settings.models, hosted=settings.models.generation_backend == "api"
    )


def detector() -> PiiDetectorPort:
    # The same detector the ingestion gate runs. An answer must not be able to disclose what
    # ingestion would have refused to publish (INV-7).
    return PatternPiiDetector()


def chat_service(
    session: Session = Depends(get_session),
    funnel: RetrievalClient = Depends(retrieval),
    model: GenerationPort = Depends(generation),
    pii: PiiDetectorPort = Depends(detector),
) -> ChatService:
    return ChatService(retrieval=funnel, generation=model, pii=pii, audit=SqlAuditSink(session))


# ------------------------------------------------------------------------------ rate limit


class RateLimiter:
    """Per-principal request budget, in memory.

    Deliberately local to the process: this is a guard against one caller looping, not a
    distributed quota. The public surface's real limit lives at the DMZ gateway (M8), and a
    limiter that pretended otherwise would be trusted for something it cannot do.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int) -> None:
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= limit:
            raise RateLimited("too many requests", retry_after=60)
        window.append(now)


_limiter = RateLimiter()


def limiter() -> RateLimiter:
    return _limiter


# ---------------------------------------------------------------------------- the caller


class Caller:
    """A verified principal and the token to retrieve with — not always the same token."""

    def __init__(self, principal: Principal, token: str) -> None:
        self.principal = principal
        self.token = token


def bearer(authorization: str = Header(default="")) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("a bearer token is required")
    return token


def current_caller(
    token: str = Depends(bearer),
    on_behalf_of: str = Header(default="", alias=ON_BEHALF_OF_HEADER),
    verify: TokenVerifier = Depends(verifier),
    exchange: TokenExchanger = Depends(exchanger),
) -> Caller:
    principal = resolve_principal(token, verify)

    # A service account asking on someone's behalf: exchange first, then become the delegate.
    if principal.kind is PrincipalKind.SERVICE:
        if not on_behalf_of:
            raise PolicyViolation(
                "a service account must name the user it is acting for",
                principal=principal.subject,
                invariant="INV-3",
            )
        exchanged = exchange.exchange(token, user_subject=on_behalf_of)
        delegated = resolve_principal(exchanged, verify)
        if delegated.on_behalf_of is None:
            # The exchange returned a token without an `act` claim, so nothing records the
            # delegation. Answering with it would attribute a user's reads to nobody.
            raise PolicyViolation(
                "exchanged token does not record the delegation",
                principal=principal.subject,
                invariant="INV-3",
            )
        return Caller(delegated, exchanged)

    # A user asking for themselves, or a bot that already exchanged. Their token goes to the
    # funnel unchanged: chat-api has no authority to assert an identity of its own (INV-2).
    return Caller(principal, token)


# ------------------------------------------------------------------------------ endpoint


@app.post("/v1/chat/{surface}", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    surface: str = Path(pattern="^(internal|external)$"),
    caller: Caller = Depends(current_caller),
    service: ChatService = Depends(chat_service),
    rate: RateLimiter = Depends(limiter),
) -> ChatResponse:
    check_deployment_surface(surface)
    policy: SurfacePolicy = policy_for(surface)
    rate.check(f"{surface}:{caller.principal.audit_actor}", policy.rate_limit_per_minute)
    return service.answer(body, principal=caller.principal, token=caller.token, policy=policy)


@app.get("/v1/chat/{surface}/policy")
def surface_policy(
    surface: str = Path(pattern="^(internal|external)$"),
    _caller: Caller = Depends(current_caller),
) -> dict[str, Any]:
    """What this surface will do — for the UI, and for a reviewer asking what differs."""
    check_deployment_surface(surface)
    policy = policy_for(surface)
    return {
        "surface": policy.surface.value,
        "top_k": policy.top_k,
        "max_answer_tokens": policy.max_answer_tokens,
        "history_turns": policy.history_turns,
        "rate_limit_per_minute": policy.rate_limit_per_minute,
        "expand_graph": policy.expand_graph,
        "refusal": policy.refusal,
    }


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
