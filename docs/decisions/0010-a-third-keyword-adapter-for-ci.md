# ADR-0010 — A Postgres full-text adapter for CI, outside the bake-off

**Status:** accepted · **Date:** 2026-08-11

## Context

[OPEN]-2 is OpenSearch vs ParadeDB `pg_search`, decided by the M7 bake-off. Neither can run in
CI as it stands: OpenSearch needs a node, and the bake-off has not happened. Without a keyword
backend, CI cannot exercise the publish → index → retrieve path at all — which means the ACL
sweep, the invariant that actually matters, would only ever run against the vector index.

## Decision

A third `KeywordIndexPort` adapter over Postgres full-text search (`to_tsvector`/`ts_rank_cd`
with an immutable `kb_unaccent` wrapper). It is the default when `KB_KEYWORD_BACKEND` is not
`opensearch`, and it declares `bakeoff_candidate: False` in its `info`.

It is explicitly **not** a candidate: `ts_rank_cd` is not BM25, `simple` does no Vietnamese
segmentation, and it shares Postgres's resources with the registry.

Query terms are OR-ed rather than AND-ed, matching the OpenSearch adapter's
`minimum_should_match: 1`. `websearch_to_tsquery` ANDs everything, which on a corpus where the
answer is usually one clause is a recall disaster.

## Consequences

CI runs the whole funnel, including the ACL sweep and the recall gate, with no extra service.
The cost is a third implementation of "compile the ACL into this engine's query" — mitigated
by all three compiling from the same `kb_authz.compile` functions, so the predicate itself has
one definition and one set of tests.

Similarly, `kb_ports.adapters.embedding_hashed` provides a deterministic lexical embedding so
the vector path runs without a GPU. Both adapters state in their `info` that they are not
semantic, so an eval report can never be mistaken for a production measurement.
