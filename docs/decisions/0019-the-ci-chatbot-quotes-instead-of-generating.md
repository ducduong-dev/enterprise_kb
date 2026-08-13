# ADR-0019 — The CI chatbot quotes instead of generating

**Status:** accepted · **Date:** 2026-08-11

## Context

M6's acceptance criterion is a faithfulness score over 60 questions and 20 red-team prompts.
Running it against a real generation model on every CI run would mean grading a stochastic
process: the same commit scores differently on Tuesday, a threshold gets lowered to stop the
noise, and the gate stops meaning anything. It would also require a GPU in CI, which the rest
of the pipeline has been careful not to need (ADR-0010, ADR-0011).

The properties the criterion is actually about are not the model's:

* nothing outside the retrieved context can reach the answer;
* every claim carries a citation that resolves to a version;
* an empty or off-topic context produces a refusal;
* the PII filter runs on the way out;
* every outcome — answer, refusal, redaction — is logged well enough to reconstruct.

Those belong to the pipeline, and they must hold every time, not on average.

## Decision

`ExtractiveGeneration` is the CI generation adapter. It reads the numbered passages back out
of the prompt it was given and returns the ones whose vocabulary overlaps the question, each
followed by its `[n]` marker: a genuinely grounded answer with genuinely valid citations,
perfectly faithful and perfectly unhelpful.

It declares `real_model: False`, and the harness prints which adapter produced its numbers.
`eval/harness/answers.py --generation vllm` runs the identical criteria against the real model.

Two consequences are recorded rather than hidden:

* **Questions requiring semantic matching are not scored.** A question asked in English against
  a Vietnamese instrument matches only semantically, and the CI embedding is a lexical
  projection (`semantic: False`). Those entries carry `requires: semantic` and are reported as
  *not measured*, so a green run never implies a capability that was never exercised.
* **Declining is judgement.** A red-team prompt that discloses nothing but should have been
  declined ("print your system prompt") is scored separately from a disclosure. Disclosure —
  a canary token, a document the principal may not read, a personal identifier — fails the run
  always, because that is a property of the filter and not of the model. Non-refusal becomes
  blocking as soon as a real model is configured.

## Consequences

* The M6 gate runs in ~20 seconds on CPU and gives the same answer every time.
* The security half of the red team is enforced on every commit; the conduct half needs the
  GPU stack, and the report says so rather than quietly passing.
* A faithfulness figure in this repository always carries the name of the adapter that
  produced it. Without that label the number is not a measurement of anything.
