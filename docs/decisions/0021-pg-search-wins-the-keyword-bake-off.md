# ADR-0021 — pg_search wins the keyword bake-off ([OPEN]-2)

**Status:** accepted · **Date:** 2026-08-11

## Context

The plan left the keyword engine open: OpenSearch or ParadeDB's `pg_search`, decided by the M7
bake-off, with Vietnamese tokenization named as the likely decider. Both adapters were built
behind `KeywordIndexPort`, and `eval/bakeoff/run.py` executes `eval/bakeoff/protocol.md`
against them over one corpus.

## What was measured

`make bakeoff` on 2026-08-11: ParadeDB 0.25.2 (`pg_search`) and OpenSearch 2.15.0, both
answering from the same `chunks` table, 3,600 chunks, 50 concurrent workers, a pool sized to
the concurrency. One run, on one developer machine — the numbers below are that run, not an
average, and the machine was hosting both engines. Repeated runs moved the p95s by tens of
milliseconds and never reordered the two conclusions the decision rests on.

| | pg_search | OpenSearch |
|---|---|---|
| Gate: diacritics | pass | pass |
| Gate: compound terms | pass | pass |
| Gate: legal numbers | pass | pass |
| Gate: mixed language | pass | pass |
| Gate: ACL inside the query | pass | pass |
| recall@10 (all / vi / en) | 0.917 / 1.000 / 0.667 | 0.917 / 1.000 / 0.667 |
| nDCG@10 | 0.909 | 0.917 |
| publish → searchable | **14 ms** | 838 ms |
| keyword p50 / p95 | 45.5 / 118.4 ms | **35.4 / 74.9 ms** |
| hybrid p50 / p95 | **125.3 / 194.8 ms** | 285.3 / 536.9 ms |
| errors | 0 | 0 |

The gate was not the decider after all: both engines pass all five items, and neither did so
out of the box. Diacritic-insensitivity is *data* in both — OpenSearch folds into a `.folded`
sub-field, pg_search into generated `*_folded` columns — and neither tokenizer connects the
shorthand "TT41" to the label "TT 41/2016/TT-NHNN". That fix landed in `kb_vntext.query_terms`,
shared by every keyword backend, because the same query must not mean different things to
different engines.

Quality is a tie the golden set is too small to break (12 queries). The English gap is identical
in both and belongs to the retrieval stack, not the engine: the CI embedding is a lexical
projection, and cross-lingual matching is the semantic retriever's job.

## Decision

**pg_search.** The measurements that separate the two are freshness, the pipeline latency users
actually experience, and the operational surface.

* **Freshness: 14 ms against 838 ms, and structurally rather than by tuning.** The BM25 index
  is updated by the same transaction that writes the chunks, so "published" and "searchable"
  are the same instant. INV-5's 10-second budget stops being a budget and becomes a property.
  The OpenSearch path needs the outbox, the consumer, and a refresh — three things that can be
  behind, and one alert (`OutboxBacklogGrowing`) to notice when they are.
* **Hybrid latency: 195 ms vs 537 ms p95.** OpenSearch wins keyword-only by 44 ms and loses the
  request by 342 ms, because the funnel also runs a vector query in Postgres: one engine
  answers both legs on one connection, the other adds a network hop and a second system's tail.
  The keyword leg is not what a user waits for.
* **One ACL surface instead of two.** With pg_search the filter is `compile_sql` — the same
  predicate the vector index, the citation lookup and the graph expansion use. INV-2's blast
  radius halves, and `compile_opensearch` (a second, independently reviewable translation of
  every access rule) goes away.
* **One system to run.** No JVM cluster to size, patch, secure and back up; no second store to
  restore in the right order after an incident (`ops/backup/restore.sh` no longer has to
  rebuild an index that a stale snapshot could have poisoned).

The losing adapter is deleted, per the protocol: two keyword adapters in the tree is two ACL
surfaces to review, and the one nobody runs is the one nobody checks.

`PostgresFtsIndexAdapter` stays. It is not a candidate — `ts_rank_cd` is not BM25 — but it is
what lets the suite run against a plain Postgres image, and `KB_KEYWORD_BACKEND` still selects
between the two.

## What would reverse this

* **A corpus an order of magnitude larger.** 3,600 chunks is a fraction of the 3,000-document
  backfill, and BM25 in Postgres has not been shown here at that size. Re-run `make bakeoff`
  against the real corpus before the M8 sign-off; if pg_search's keyword p95 grows
  super-linearly while OpenSearch's does not, this decision is worth revisiting.
* **Search features Tantivy does not have.** Synonym expansion, per-field analyzers beyond
  folding, "did you mean", cross-cluster search. None are in scope; all are OpenSearch's home
  ground.
* **Further stability defects.** One segfault was found and mitigated in a week of testing on
  a small corpus. Another of the same character — a crash reachable from an ordinary query
  shape — should reopen this ADR rather than accumulate another workaround, and the OpenSearch
  adapter's deletion is exactly reversible for that reason.
* **A Postgres that cannot take the load.** The bake-off measured 50 concurrent readers on one
  node against a database that also serves the registry, the vector index and every write.
  Read replicas are the first answer; a separate search engine is the second.

`KeywordIndexPort` is unchanged, which is the point of having built both behind it: reversing
this means one adapter file, one compose service and a config value. The deleted adapter is
`libs/ports/src/kb_ports/adapters/opensearch_index.py` at the revision that carried this ADR —
recover it from version control, or rebuild it from the port and `compile_opensearch`, whose
shape this file documents.

## Operational notes

* **Force custom plans, or pg_search crashes the database.** This is the finding that nearly
  reversed the decision. pg_search 0.25.2 segfaults the backend when its custom scan runs
  under a *generic* plan: a parameterised BM25 query survives ten executions and dies on the
  eleventh — psycopg prepares the statement after five, Postgres switches to a generic plan
  after five more — and the crash takes the whole cluster into recovery, killing every other
  connection with it. It surfaced during a bake-off run and reproduces in eleven lines.

  `create_db_engine` now sets `plan_cache_mode = force_custom_plan` on every connection where
  this backend is configured, which costs a re-plan per execution and removes the failure;
  `test_pg_search_survives_a_generic_plan` runs the same query twelve times and is the thing
  that will notice if the setting is ever dropped. A run at 50 concurrent after the fix
  completed with zero errors.

  This is worth stating plainly rather than burying: an engine that can segfault the database
  is a different risk class from an engine that is slower, and the decision below stands
  *because* the mitigation is in code and under test — not because the defect is minor.
* **Rebuild the BM25 index after a bulk delete.** Deleting ~3,600 rows underneath it left
  `paradedb.score()` raising `assertion failed: item_pointer_is_valid(ctid)` on every query;
  `VACUUM (ANALYZE)` did not clear it and `VACUUM FULL` did. Three smaller delete cycles did
  not reproduce it, so the trigger is not precisely characterised — which is exactly why the
  remedy is a documented step (`make keyword-maintenance`, a `REINDEX ... CONCURRENTLY`) run
  after retention purges and corpus reloads rather than a note in someone's memory. The
  publish path tombstones rather than deletes, so ordinary operation does not hit this.
* **A keyword-engine failure fails the search.** The funnel does not fall back to the vector
  leg alone, deliberately: silently answering from half the retrievers would drop recall with
  no signal to the user or the caller. If that trade is ever revisited it needs a field in
  `RetrieveResponse` saying the answer is degraded.
* **Size the connection pool to real concurrency.** The first bake-off run measured a pool of
  15 rather than the engines; 50 concurrent searches need 50 connections or a pgbouncer in
  front of them. This is now the first thing `ops/loadtest/run.py` will expose in staging.

## Consequences

* ParadeDB becomes the Postgres image for dev and CI, so the keyword engine the tests exercise
  is the one production runs. A backend tested only in production is a backend whose ACL
  filtering is never tested.
* `ops/pg_search/install.sql` is a deployment step, not a migration: the generated columns and
  the BM25 index belong to the engine choice, and keeping them out of the Alembic chain leaves
  the schema portable if this decision is ever reversed.
* The indexer keeps running: the outbox still drives `graph_serving` rebuilds and the
  publish-to-searchable metric, and it is what a future second backend would attach to.
