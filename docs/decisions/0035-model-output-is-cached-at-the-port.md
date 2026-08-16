# ADR-0035 — Model output is cached at the port, keyed by the prompt that produced it

**Status:** accepted · **Date:** 2026-08-13 · **Implemented:** 2026-08-16
(`kb_ports.adapters.generation_cached`, migration 0011).

## Context

Nothing in the platform caches a model response. Not the ports, not the LiteLLM proxy config —
every call is billed and waited for, every time, including the ones we have already made.

Four workloads ask the same question repeatedly:

* **ADR-0033's backfill.** Clause-supersession detection over 3,000 documents, where a
  frequently-restated clause is adjudicated against the same predecessor from many candidates.
* **Rechunking.** `PublishService.rechunk` replaces a version's chunks when the chunker or the
  embedding model improves. The chunk ids all change; most of the *text* does not. Every
  detection that ran against that text runs again.
* **Evals.** `eval/` runs fixed fixture sets in CI, on every merge, against prompts that changed
  in maybe one of twenty runs.
* **Merge drafts.** A regenerated draft re-drafts sections whose before-and-after are unchanged.

None of this is a throughput problem today, because there is no GPU node attached. It becomes
one on the day there is, and the backfill is the first thing that will run.

## Decision

A caching decorator around `GenerationPort` — INV-12 already puts everything model-shaped behind
a port, so this is a wrapper, not a new concept — keyed by a hash of **everything that can
change the answer**:

```
sha256(model_id ‖ prompt_version ‖ rendered_prompt ‖ temperature ‖ max_tokens)
```

Stored in Postgres, as a table.

Three constraints:

**Only deterministic calls are cached.** `temperature > 0` means the caller is asking for
variation; serving it a stored answer silently defeats the request. The wrapper passes those
straight through.

**The prompt version is in the key, and the row records it.** This is the property the whole
design turns on: a cached judgement is only valid for the prompt that produced it. A test
asserts that editing a prompt file changes the key, because the failure it prevents — today's
prompt served yesterday's verdict — is invisible in every output the system produces.

**A miss is never an error.** The cache is an optimisation with no semantics of its own;
eviction is size-based and dropping the whole table changes nothing but the bill.

## Why not the proxy

LiteLLM can cache, and it is the wrong place. The key it can compute is the request body, and it
does not know what a prompt *version* is — so a cache there survives an edit to
`merge_classifier.md` and keeps answering with the old prompt's verdicts. Correctness of a cached
judgement depends on facts the proxy cannot see.

There is a second reason. Cached content is bank document text and the model's findings about it.
That belongs in the audited database, under the same backup and retention rules as everything
else, not in a sidecar's key-value store that nobody inventories.

## Consequences

* CI evals get faster and cheaper, and stay honest: a prompt change invalidates exactly the
  entries it should, and the eval re-runs for real.
* The 3,000-document backfill becomes re-runnable. Today, re-running it means paying for it
  again, which in practice means people avoid re-running it and work from stale results.
* One more table to back up and to include in the retention schedule (`[OPEN]`-4).
* The cache must not become a way to serve a model answer after the underlying document changed.
  It cannot: the document text is *in* the rendered prompt, so different text is a different key.
  That is why the key hashes the rendered prompt rather than a document or chunk id.
