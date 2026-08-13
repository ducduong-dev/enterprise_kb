# ADR-0024 — Model traffic goes through one proxy

**Status:** accepted · **Date:** 2026-08-11

## Context

By M8 the platform called four kinds of model over three protocols: vLLM's OpenAI-compatible
chat completions for generation and vision, text-embeddings-inference's `/embed` for
embeddings, and its `/rerank` for reranking. Each adapter held its own base URL, its own
credential handling, and its own idea of what an error looked like.

That works while every model is a local server the platform owns. It stops working the moment
any of these questions is asked, and all of them were:

* what does the bank spend on models, per service, per month?
* which key is used where, and how is it rotated?
* can a scanned page be sent to a public vision model, and if so *which* pages went?
* when the GPU node is down, does the answer fail or fall back — and to what?
* can we try a different model without shipping code?

Each answer would otherwise be built four times, once per adapter, and each one differently.

## Decision

All model calls go through a LiteLLM proxy. The platform knows four **roles** — generation,
vision, embedding, rerank; `ops/litellm/config.yaml` maps each role's alias to a provider.

* `kb_ports.proxy.route(role)` resolves a role to a base URL, a model alias, a credential, and
  whether that route leaves the bank's network. Every adapter asks it, so the four roles share
  one endpoint and one key.
* The adapters keep their direct paths (`KB_MODEL_USE_PROXY=false`), because a deployment that
  has not adopted the proxy must keep working, and because the direct path is what the proxy is
  measured against.
* Protocol per role stays honest: chat-completions for generation and vision, OpenAI
  `/v1/embeddings` and Cohere-shaped `/v1/rerank` when proxied, TEI's own shapes when not.
  `libs/ports/tests/test_proxy_adapters.py` pins both.

The proxy is *infrastructure*, not an abstraction the platform can hide behind. Two things
stayed on this side of the line deliberately:

* **The decision to send documents outside.** The proxy will route wherever it is configured;
  `route` refuses an external model unless `KB_MODEL_ALLOW_EXTERNAL_PROCESSING` is set, and it
  refuses at start-up rather than on the first document ([OPEN]-1). A proxy misconfiguration
  therefore fails loudly here instead of quietly at a provider.
* **Recording where a call went.** `KB_MODEL_EXTERNAL_MODELS` mirrors the routes in the proxy
  config that leave the network, so every adapter's `info` — and from there the IDP report and
  the answer's audit record — says truthfully whether the bank's content left the building.
  That duplication is deliberate: the alternative is asking the proxy at call time and
  trusting its answer about itself.

## Consequences

* Changing which model serves a role is a line in `ops/litellm/config.yaml` and a restart.
  Nothing learns a new protocol, and no adapter changes.
* One credential store, one place for rate limits and spend, one health check. Per-service
  virtual keys make "the DMZ chatbot" and "the ingestion workflow" separate tenants of the same
  proxy.
* One more thing to run, and a new single point of failure in the model path. Mitigated by
  LiteLLM's own fallbacks (configured to fall back on-premises, never outward — a public
  fallback would turn an outage into an unreviewed disclosure) and by keeping the direct path
  working.
* `turn_off_message_logging` is on. The proxy sees every document and every answer; the
  platform's audit trail is where that gets recorded (INV-11), and a second copy inside a
  proxy's logs is a second place to leak from.
