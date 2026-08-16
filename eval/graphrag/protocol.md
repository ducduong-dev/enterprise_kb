# Graph-RAG evaluation — Graphiti and LightRAG against M9

Asks whether either framework should be adopted to solve M9 (expiry, ADR-0030/0031) and
clause-level supersession (ADR-0032/0033), rather than building the ledger and the detector in
the platform. Same shape as `eval/bakeoff/protocol.md`: a gate first, scores only for what
passes.

Versions examined: **graphiti-core 0.29.3** (Apache-2.0) and **LightRAG 1.5.7** (MIT), read at
source on 2026-08-13.

## Gate (pass/fail, evaluated first)

The platform's invariants are not negotiable for a component that sits on the read path, so
they are the gate. An entry failing any item is out regardless of retrieval quality.

| # | Gate | Graphiti | LightRAG |
|---|---|---|---|
| G1 | **The citable unit is a clause.** The thing retrieval returns can be cited as "Điều 12.2, TT 41/2016/TT-NHNN" (ADR-0008, ADR-0018). | **fail** — the unit is an LLM-extracted triple whose provenance is an *episode*, not an article | **fail** — the unit is a token-window chunk spanning several articles (measured below) |
| G2 | **ACL inside the query, per principal** (INV-2): visibility plus group unlock, compiled into the index query. | **fail** — `group_id` partitions a graph per tenant; there is no per-principal visibility/group predicate | **fail** — no per-principal filter on the retrieval path |
| G3 | **One Postgres transaction** for canonical flip, chunks, edges and outbox (INV-5). | **fail** — requires Neo4j, FalkorDB, Kuzu or Neptune; no Postgres driver exists | **pass** — a Postgres/pgvector backend is supported |
| G4 | **A machine may propose, only a human confirms**, for regulated classes (INV-8, ADR-0033). | **fail** — invalidation is automatic and unreviewable; there is no proposed/confirmed state anywhere in the codebase | n/a — no invalidation to review |
| G5 | **Temporal validity is representable at all** — a fact can cease to apply on a date. | **pass** — `valid_at`, `invalid_at`, `created_at`, `expired_at` on every edge, with date filters in `SearchFilters` | **fail** — no temporal, validity or versioning concept in the core modules |

Neither entry reaches the scored comparison. G1 and G2 are the decisive ones and they are
architectural: they would be failed by any framework whose retrievable unit is a machine-derived
fact rather than a clause of a document the bank holds.

## What was measured, and what could not be

The machine running this evaluation has no model node — the LiteLLM proxy is not up on :4000 and
`KB_MODEL_DETERMINISTIC_FALLBACK=true`. **No retrieval-quality comparison was run**, and none of
the numbers below depend on a model. What was measured is the unit of work each design commits
to, which is deterministic: chunking, and the LLM call fan-out that follows from it.

Corpus: the two real instruments in `data/` — Nghị định 309/2026/NĐ-CP (which amends
118/2025/NĐ-CP) and Nghị định 118/2025/NĐ-CP.

| | NĐ 309/2026 | NĐ 118/2025 |
|---|---:|---:|
| Source size (chars) | 16,170 | 94,367 |
| **LightRAG** chunks (`chunking_by_token_size`, defaults) | 5 | 24 |
| — chars per chunk | 3,234 | 3,932 |
| — extraction LLM calls (`MAX_GLEANING=1`) | 10 | 48 |
| — input tokens, extraction only, floor | 10,240 | 56,238 |
| **Platform chunker** (`kb_indexer.chunker`) | 32 | 208 |
| — chars per chunk | 505 | 454 |
| — chunks carrying a citation label | 32 / 32 | 208 / 208 |
| — distinct articles (Điều) | 15 | 41 |
| — extraction LLM calls | 0 | 0 |

Extrapolated to the 3,000-document backfill at the mean of these two documents: **≈87,000
extraction LLM calls and ≈100M input tokens** for LightRAG, before any merge summarisation, and
that is a floor. Graphiti is structurally more expensive: `resolve_extracted_edges` fans out one
`resolve_edge` LLM call **per extracted edge**, plus a timestamp call per edge with no date and
two graph searches per edge, on top of node extraction, edge extraction and node dedupe per
episode. Its cost scales with facts extracted, not with text ingested.

Against which, measured on the same two documents with no model calls at all, the platform's
existing Vietnamese text utilities already return every input M9a needs:

| | NĐ 309/2026 | NĐ 118/2025 |
|---|---|---|
| Legal number | `309/2026/NĐ-CP` | `118/2025/NĐ-CP` |
| Issue date | 2026-08-05 | 2025-06-09 |
| Effective date | 2026-08-05 | 2025-07-01 |
| Evidence sentence | *"Nghị định này có hiệu lực thi hành từ ngày 05 tháng 8 năm 2026"* | *"…từ ngày 01 tháng 7 năm 2025"* |
| Typed references | 13 (`amends`, `cites`) | 10 (`abrogates`, `amends`, `implements`, `cites`) |

## The one experiment still worth running

The gate settles adoption. It does not settle whether Graphiti's *adjudicator* is better than the
one ADR-0033 specifies, and that is a real question with a cheap answer. Run on the GPU node,
when models are available:

1. Build a fixture set of clause pairs from the corpus: confirmed supersessions, restatements
   with no change of rule, and — the ones that matter — pairs that read alike and apply to
   different products or customer segments.
2. Run three adjudicators over the same pairs: ADR-0033's four-bucket prompt; Graphiti's
   `resolve_edge` prompt (`duplicate_facts` / `contradicted_facts`, ported verbatim); and a
   naive "newer wins" baseline that consults only the effective dates.
3. Report precision and recall per bucket, and separately the **false-supersession rate** on the
   different-scope pairs. That is the number the design turns on: Graphiti's prompt has no
   abstention output and its `resolve_edge_contradictions` then invalidates purely on `valid_at`
   ordering, so a different-scope pair is expected to resolve as a contradiction. Measuring by
   how much is worth the afternoon.
4. If Graphiti's prompt wins on the superseded bucket, port its examples into the platform's
   prompt rather than its architecture.

## Re-opening

This evaluation is about M9. It does not settle whether a graph-RAG layer is worth having for
*corpus-wide questions* — "what does the bank say about topic X across 3,000 documents" — which
is what LightRAG's local/global retrieval is actually built for and what the `feat/graph_rag`
branch is exploring. That is a separate question with a separate protocol, and G1/G2 would still
apply to anything that reaches an end user.
