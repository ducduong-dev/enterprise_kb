# Keyword engine bake-off — OpenSearch vs ParadeDB `pg_search`

Resolves [OPEN]-2. Runs in M7, decided before M8. The outcome is committed as an ADR and the
losing adapter is deleted — two keyword adapters in the tree is two ACL surfaces to review.

## Gate (pass/fail, evaluated first)

Vietnamese tokenization is the likely decider, so it is a gate rather than a score:

1. **Diacritics.** `an toàn vốn` and `an toan von` both retrieve the same article; `vốn` does
   not match `von` when the query itself is diacritic-correct (no silent normalization loss).
2. **Compound terms.** `tỷ lệ an toàn vốn` ranks the article above documents merely
   containing `vốn`.
3. **Legal numbers.** `41/2016/TT-NHNN`, `TT41`, `Thông tư 41` all reach the same document.
4. **Mixed-language documents.** A vi+en document is retrievable from either language.
5. **ACL filter inside the query** (INV-2), with an execution plan showing the filter applied
   before scoring, not after.

An engine failing any gate item is out regardless of its scores.

## Scored comparison (identical corpus, identical golden set)

| Dimension | Measurement |
|---|---|
| Quality | recall@10, nDCG@10 over the golden set, per language |
| Latency | p50/p95 keyword-only, and within the full hybrid pipeline, at 50 concurrent |
| Freshness | publish → searchable, measured against the 10 s budget (INV-5) |
| Operations | backup/restore drill, single-node failure recovery, disk growth per 1k documents |
| Complexity | services to run, ACL code paths to review, dependency surface |

## Protocol

1. Load the same 3,000-document corpus into both adapters from the same chunk stream.
2. Run `eval/harness/run.py` against each with the same principals — quality *and* ACL sweep.
3. Run the load profile at 50 concurrent for 15 minutes; record p95 and error rate.
4. Kill each engine mid-publish; confirm outbox recovery leaves no divergence (INV-5/6).
5. Write the ADR: gate results, scores, decision, and what would reverse it.
