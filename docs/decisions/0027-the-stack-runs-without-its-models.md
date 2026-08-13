# ADR-0027 — The stack runs without its models, and says so

**Status:** accepted · **Date:** 2026-08-12

## Context

Bringing the whole platform up on a machine with no GPU node was impossible, and nobody had
noticed because the tests never bring it up: they construct services in-process with the
deterministic adapters injected.

There were two settings and neither was the one needed. `KB_ENV=dev` selects the real model
adapters, so the first publish waits on an embedding server that is not there. `KB_ENV=test`
selects the deterministic ones — and also redirects object storage to a local directory, so
portal-api writes the original into MinIO and the workflow worker looks for it on its own
filesystem. The visible symptom is an upload that dies in `parsing` with "object not found",
which reads like a storage bug and is not one.

The environment name was doing two unrelated jobs: *which models do we call* and *which
infrastructure is real*.

## Decision

Split them. `KB_MODEL_DETERMINISTIC_FALLBACK` selects the deterministic embedding, rerank and
generation adapters, independently of `KB_ENV`. Storage, auth, the database, Temporal and the
workflows stay exactly as configured.

`Settings.use_deterministic_models` is the single predicate the services ask — `env == "test"
or models.deterministic_fallback` — so there is one answer to "are we calling a model", and
`get_settings()` refuses the flag in staging and production the same way it refuses insecure
tokens.

The adapters it selects already declare what they are: `semantic: false` on the hashed
embedding and lexical rerank, `real_model: false` on the extractive generation. An eval run
made under this flag therefore reports itself as not-measured rather than passing quietly —
which is the property that makes the switch safe to offer at all.

Where a model-shaped dependency has no deterministic stand-in — OCR, the vision model — the
worker leaves it unset, and an activity that needs one fails saying so. A scanned document
under this flag is an error, not a transcription nobody can trust.

## Consequences

* A developer with no GPU runs `make dev-all` with one line changed in `.env`, and everything
  except model quality behaves like production: real MinIO, real Keycloak, real Temporal, real
  ParadeDB, the real publish transaction.
* Retrieval under the flag is keyword-strong and semantically meaningless. That is honest
  rather than hidden, but it means "search seems worse locally" has an answer before anyone
  investigates it as a regression.
* Two settings can now disagree — `KB_ENV=dev` with the flag on is a real deployment calling
  no models. That is the intended combination, and the one the flag exists to make expressible.
* One more thing to get wrong in production. It is refused there, by the same guard that
  refuses `KB_OIDC_ALLOW_INSECURE_TOKENS`, so a mistake fails at start-up rather than serving
  hashed vectors to a bank.
