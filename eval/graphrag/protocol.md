# Graph-RAG evaluation — Graphiti and LightRAG against M9

Asks whether either framework should be adopted to solve M9 (expiry, ADR-0030/0031) and
clause-level supersession (ADR-0032/0033), rather than building the ledger and the detector in
the platform. Same shape as `eval/bakeoff/protocol.md`: a gate first, scores only for what
passes.

Versions examined: **graphiti-core 0.29.3** (Apache-2.0) and **LightRAG 1.5.7** (MIT), read at
source on 2026-08-13. Graphiti's temporal machinery was re-read at source on **2026-08-17**, same
version, checked out at `/home/clt/workspace/mbbank/graphiti`; that pass added G6 and G7 below and
four notes to ADR-0033. It did not change the adoption answer.

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
| G6 | **Temporal validity is enforced on the read path by construction**, not by the caller remembering to ask (INV-1/2). | **fail** — filtering is opt-in via `SearchFilters` (`search/search_filters.py:62-65`); the default path returns expired facts and hands the model their dates with a note asking it to reason (`search/search_helpers.py:25-57`) | n/a — nothing to enforce |
| G7 | **An expiry decision can be reversed** — a steward who was wrong can restore the clause (ADR-0030's `revoked`). | **fail** — `expired_at` is set once and never cleared anywhere in the codebase; invalidation is not a state, so there is nothing to revoke | n/a |

Neither entry reaches the scored comparison. G1 and G2 are the decisive ones and they are
architectural: they would be failed by any framework whose retrievable unit is a machine-derived
fact rather than a clause of a document the bank holds.

G6 and G7 were added on the second reading and are worth stating separately from G4 even though
Graphiti fails all three. G4 says a machine decides without review; G7 says the decision cannot be
undone afterwards, which is the harder problem — a review queue can be added to a design that is
reversible, and cannot rescue one that is not. G6 is the one to keep in mind for any future
graph-RAG layer (see *Re-opening*): a component can be perfectly temporal in its storage and still
serve an expired rule, because the filter is a thing the caller opts into rather than a predicate
compiled into the query.

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
   different-scope pairs. That is the number the design turns on. Refined by the second reading:
   Graphiti's prompt does have an abstention *case* — its third example returns two empty lists
   for two facts that differ in context rather than in truth (`prompts/dedupe_edges.py:94-96`) —
   but no abstention *output*, so "different scope" and "found nothing" are the same answer, and
   `resolve_edge_contradictions` then invalidates purely on `valid_at` ordering
   (`utils/maintenance/edge_operations.py:565-571`). A different-scope pair is still expected to
   resolve as a contradiction; what the port must reproduce faithfully is the empty-list
   convention, or the comparison flatters us.
4. If Graphiti's prompt wins on the superseded bucket, port its examples into the platform's
   prompt rather than its architecture. Two are already scheduled for porting on their face
   regardless of the outcome — the numeric-difference instruction and the different-context
   example — so the experiment's real question is narrower than it looks: whether anything in the
   *rest* of that prompt beats the four-bucket framing.

## What was taken instead

Rejecting a framework is not the same as learning nothing from it. Graphiti solves a genuinely
harder version of the direction problem — it has no legal dates, no instrument ranks and no
declaring sentences — and everything below is borrowed with the ADR that took it. Kept here as
one list so a future reader can see the borrowings without re-reading seven ADRs.

| Taken | From | Where |
|---|---|---|
| Two clocks on every row: world time and belief time, append-only, invalidate-never-delete | `EntityEdge.valid_at`/`invalid_at` vs `created_at`/`expired_at` (`edges.py:271-281`) | ADR-0030 |
| Disjoint-window guard before anything else | `resolve_edge_contradictions` (`edge_operations.py:554-561`) | ADR-0033, gate 2 |
| A model answers with indices into a list it was given, never with echoed keys | `EdgeDuplicate.duplicate_facts` / `contradicted_facts` | ADR-0034 |
| *Never* call two facts the same when a numeric value, date or qualifier differs | `resolve_edge` (`prompts/dedupe_edges.py:53`) | ADR-0033, gate 5 |
| The different-context abstention example, translated to a clause pair | `prompts/dedupe_edges.py:94-96` | ADR-0033, gate 5 |
| The model nominates, a pure date function disposes | the split between `resolve_edge` and `resolve_edge_contradictions` | ADR-0033, gate 5 |
| Detection must be able to point *backwards*, because the older text often arrives second | `resolve_extracted_edge` expiring the incoming edge (`edge_operations.py:825-839`) | ADR-0033 |
| Validity dates travel to the model as structured fields beside the passage | `search_results_to_context_string` (`search/search_helpers.py:25-57`) | ADR-0033 |
| Read the stated date; only call a model when none was stated, and never let it invent one | `_extract_edge_timestamps`' early return (`edge_operations.py:587`) and its prompt's *"NEVER hallucinate dates"* | independently arrived at in ADR-0013/0029, confirmed here |

The last row is the useful negative result: on the one question both designs answer the same way,
Graphiti reached the same rule from the opposite starting point. That is worth more confidence in
the rule than either design alone gives.

## Re-opening

This evaluation is about M9. It does not settle whether a graph-RAG layer is worth having for
*corpus-wide questions* — "what does the bank say about topic X across 3,000 documents" — which
is what LightRAG's local/global retrieval is actually built for and what the `feat/graph_rag`
branch is exploring. That is a separate question with a separate protocol, and G1/G2 would still
apply to anything that reaches an end user.
