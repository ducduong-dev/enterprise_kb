"""Where a model call actually goes (ADR-0024).

Every model-shaped call in the platform — generate an answer, transcribe a page, embed a
chunk, rerank a candidate list — is one of four *roles*. This module turns a role into a
route: a base URL, a key, a model alias, and the one fact the rest of the system needs to
record honestly, which is whether that route leaves the bank's network.

With the LiteLLM proxy in front (the default), all four roles share one endpoint and one
credential, and which provider serves them is the proxy's configuration rather than ours. That
is the point: swapping Qwen for a hosted vision model becomes a line in
`ops/litellm/config.yaml`, and nothing in the platform learns a new protocol.

The part that is *not* delegated is the decision to send a document outside. The proxy will
happily route wherever it is configured; `guard` refuses to be the one that does it, unless
Compliance has switched external processing on ([OPEN]-1). A misconfigured proxy then fails
loudly at the adapter instead of quietly at the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kb_common.config import ModelGatewaySettings, get_settings
from kb_common.errors import ConfigError
from kb_common.logging import get_logger

log = get_logger(__name__)

Role = Literal["generation", "vlm", "embedding", "rerank"]


@dataclass(frozen=True, slots=True)
class Route:
    """Where one role's calls go, and what that means."""

    role: Role
    base_url: str
    model: str
    api_key: str | None
    #: True when the provider behind this route sits outside the bank's network. Recorded in
    #: every adapter's `info`, and from there in the IDP report and the answer's audit record:
    #: "which model produced this" is a question an auditor asks years later.
    leaves_network: bool
    #: False when the call goes straight to a local server rather than through the proxy.
    proxied: bool

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def describe(self) -> dict[str, object]:
        return {
            "role": self.role,
            "model": self.model,
            "proxied": self.proxied,
            "leaves_network": self.leaves_network,
            "endpoint": self.base_url,
        }


def route(role: Role, settings: ModelGatewaySettings | None = None) -> Route:
    """Resolve a role to a route, refusing one that leaves the network without permission."""
    cfg = settings or get_settings().models
    model = _model_for(role, cfg)

    if cfg.use_proxy:
        resolved = Route(
            role=role,
            base_url=cfg.proxy_url,
            model=model,
            api_key=cfg.proxy_api_key.get_secret_value() if cfg.proxy_api_key else None,
            leaves_network=cfg.leaves_network(model),
            proxied=True,
        )
    else:
        # Direct to the model server it was configured against. `generation_backend == "api"`
        # is the pre-proxy way of saying "hosted", and it still means what it said.
        hosted = role == "generation" and cfg.generation_backend == "api"
        resolved = Route(
            role=role,
            base_url=_direct_url(role, cfg),
            model=model,
            api_key=(
                cfg.generation_api_key.get_secret_value()
                if role == "generation" and cfg.generation_api_key
                else None
            ),
            leaves_network=hosted or cfg.leaves_network(model),
            proxied=False,
        )

    guard(resolved, cfg)
    return resolved


def guard(resolved: Route, settings: ModelGatewaySettings | None = None) -> None:
    """Refuse a route that would send bank content off-premises without a ruling.

    Deliberately raised at resolution rather than at call time: a service that would send
    documents outside should fail to start, not fail on the first document.
    """
    cfg = settings or get_settings().models
    if resolved.leaves_network and not cfg.allow_external_processing:
        raise ConfigError(
            "this model route leaves the bank's network and external processing is not enabled",
            role=resolved.role,
            model=resolved.model,
            open_item="[OPEN]-1",
            remedy="set KB_MODEL_ALLOW_EXTERNAL_PROCESSING=true once Compliance has ruled",
        )
    if resolved.leaves_network:
        # Allowed, and still worth a line in the log every time a process resolves it: the
        # decision is reversible, and its consequences are not.
        log.warning("external_model_route", extra=resolved.describe())


def _model_for(role: Role, cfg: ModelGatewaySettings) -> str:
    return {
        "generation": cfg.generation_model,
        "vlm": cfg.vlm_model,
        "embedding": cfg.embedding_model,
        "rerank": cfg.rerank_model,
    }[role]


def _direct_url(role: Role, cfg: ModelGatewaySettings) -> str:
    return {
        "generation": cfg.generation_url,
        "vlm": cfg.vlm_url,
        "embedding": cfg.embedding_url,
        "rerank": cfg.rerank_url,
    }[role]
