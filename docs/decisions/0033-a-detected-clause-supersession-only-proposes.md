# ADR-0033 — A detected clause supersession only proposes

**Status:** proposed · **Date:** 2026-08-13

## Context

Two documents state the interest rate on loans to individual customers. B is from 2023, A from
2026, and neither cites the other — the archive is being digitised in whatever order it yields,
and instruments are frequently re-stated rather than formally amended. B is still in force; its
rate clause is not.

The platform does nothing about this. There is no edge, so ADR-0015's supersession flag never
fires and ADR-0032 has nothing to narrow. Both chunks are canonical, both are in force, both are
retrieved, and the reranker picks whichever reads more like the question. The answer cites a
rate the bank stopped charging three years ago, with no warning, because from the platform's
point of view nothing is wrong. This is ADR-0028's failure mode — silence that is
indistinguishable from correctness — one level down, at the clause.

The machinery that nearly does it exists and stops one step short. `diff.py` aligns clauses by
structural path and `merge.py` classifies each change as cosmetic, substantive or abrogated —
but only between two versions of *one* document, downstream of identity matching. `matching.py`
decides identity at the document level and, below its thresholds, creates a new document with no
relationship to anything. Every chunk carries a 1024-dimension embedding under an HNSW cosine
index that is only ever queried by user search.

## Decision

Clause-level supersession is **detected, stored as a proposal, and changes nothing until a
steward confirms it.** How hard the detection has to work depends on what evidence already
exists, so there are three paths, and only the last one is a search problem.

### Path A — the edge names the articles

`A --amends[articles=[12]]--> B`. There is nothing to detect: the corpus already says which
articles moved. ADR-0032 flags exactly those chunks. Detection must not run here, and must not
second-guess a confirmed edge.

### Path B — an edge exists but names no articles

The documents are known to be related; only the granularity is missing. The search space is two
documents rather than the corpus, and the prior that a matching pair is a real supersession is
far higher, so the gates below run with the subject gate relaxed and every surviving pair is
adjudicated. This is the common case for an amendment whose article list failed to parse, and it
is much cheaper than Path C.

### Path C — no edge at all

The case above. A funnel, in which **four gates run before any model is called**, each of them
cheap and each answering a question the model would otherwise be asked to guess at.

**1 · Subject candidates, from two channels.** Not cosine over the whole chunk: a chunk
embedding mixes the subject with the rule, so two clauses about lending rates that state
different rates are maximally similar — which is what we want — but so are two unrelated clauses
sharing applicability boilerplate. Each chunk carries a normalized **subject key**: the section
heading chain with instrument boilerplate stripped (`matching._TITLE_STOPWORDS` already does this
for titles), plus the item label for structured content, which for a rate schedule is
`RateItem.label` and is the strongest identifier available.

Candidates come from *either* channel — subject-key token overlap, or chunk-embedding cosine —
because they fail differently: the lexical channel misses a subject phrased in different words,
the vector channel misses nothing but admits boilerplate. This is the same instinct as the
retrieval funnel one level down, where keyword and vector are fused rather than chosen between.

**2 · Effectivity must overlap.** Two clauses whose windows never intersected cannot supersede
one another:

```
older.effective_to <= newer.effective_from  or  newer.effective_to <= older.effective_from
```

Exact and free. Borrowed from Graphiti's `resolve_edge_contradictions`, which opens with the same
guard (`eval/graphrag/protocol.md`). It lives as a pure function as well as a SQL predicate,
because the interesting cases are boundary dates and those should be testable without a database.

**3 · Scope must match.** `different_scope` is the expensive false positive, and in this corpus
scope is usually *stated*, not implied: customer segment (cá nhân / doanh nghiệp / ưu tiên),
product, currency, term, channel, and the applicability clause itself ("áp dụng đối với…"). Where
both clauses name a scope facet and the facets differ, the pair is `different_scope` — recorded
as such, with no model call. The dangerous bucket becomes deterministic wherever the text was
explicit, and only reaches the model when the text was not.

**4 · Quantities decide restatement from supersession.** For the clauses that matter most —
rates, fees, ratios, deadlines — the discriminating content is a small set of typed values. Same
subject and identical quantities is a restatement; same subject and different quantities is the
supersession signature. A quantity extractor belongs beside the date and legal-number extractors
in `kb_vntext`, patterns only, on ADR-0013's terms: it is the floor, and the model may add.

Its second job is the review screen. "8%/năm → 10%/năm" is what makes a steward's queue workable;
a similarity score is not.

**5 · Adjudication**, on what survives, one pair at a time, into four buckets:
`same_rule_restated` · `superseded` · `different_scope` · `conflicting_unresolved`. The prompt
carries an instruction taken from Graphiti's `resolve_edge`: *never treat two clauses as the same
rule restated when they differ in a numeric value, a date, or a qualifier* — for a fee or a rate
that is the whole distinction, and a model reading for gist will call them identical. The
adjudicator answers with indices rather than echoed section paths (ADR-0034).

The funnel's shape is the point. On NĐ 118/2025 — 208 chunks, measured — five candidates each
would be 1,040 pairs; gates 2 to 4 are meant to leave the model a small fraction of that, and how
small is the number the eval reports.

### Direction comes from the legal date, never from publish order

Which clause is the older one is decided by `effective_from`, falling back to the issue date.
**Never** by `created_at`, `published_at` or registry insertion order: the corpus is being
digitised in archive order, which is exactly ADR-0028's point, so publish order says nothing
about which rule came first. Where the effective dates are equal, the instrument's rank decides
(a Nghị định outranks a Quyết định), and where rank is equal too, nothing is proposed — that is a
`conflicting_unresolved` for a person.

### The record carries the date it takes effect

A confirmed supersession is not true "from now": it is true from the newer clause's
`effective_from`. Storing it without that date would break the property M9a exists to establish —
an `as_of` query before that date must still show the older clause as current, unflagged. So the
row carries `supersedes_from`, and the retrieval-side flag is evaluated against the query's
effective date like every other effectivity predicate.

### Storage

`clause_supersessions`, anchored on `(document_id, section_path)` at both ends — **never on chunk
id**. Chunk ids do not survive: `_insert_chunks` deletes and re-inserts every chunk of a version,
so any rechunk would silently orphan every row. `diff.py` learned the same lesson about
positional alignment; the structural path is the stable identifier in this corpus. The row
carries `basis` (`edge_article` | `detected` | `steward`), `supersedes_from`, the quantity delta,
the matched and mismatched scope facets, the score, the model and prompt version, `detected_by`,
`confirmed_by` and a state.

### Behaviour is graded by evidence

* `proposed` changes nothing a user can see. It is a row in a steward queue — a sixth
  `review_tasks.task_type`, `clause_review`, showing the two texts side by side with the quantity
  delta highlighted.
* `confirmed` flags the older chunk *with a pointer*: `RetrievedChunk` gains `superseded_by`, so
  the answer can say "Điều 8.2 đã được thay thế bởi Điều 5 A" rather than raising a generic
  banner. And fusion drops the older chunk when its replacement is already in the candidate set —
  without that, the answer quotes two different interest rates and leaves the customer to choose,
  which is the actual harm.
* Neither state hides the clause on the document's own page or under `as_of`. ADR-0015 flags
  rather than hides because the text is still canonical; here the argument is stronger still —
  the clause is still in the document, and a page that omits it lies about what the document says.

## Why four buckets and not a confidence score

`different_scope` and `conflicting_unresolved` are not weak instances of `superseded`. They are
different kinds of wrong, and a single score collapses them into "below threshold", where they
are dropped without a trace. The second is also a genuine finding: two in-force clauses that
contradict each other is something the bank wants to know about, and a threshold would throw it
away.

The related temptation is "newer wins, automatically" — which is precisely what Graphiti does,
and correctly, for the problem it solves. A specific older rule survives a general newer one
routinely, and a model asked "which one applies?" answers confidently either way. Whether that
judgement is ever automatic for regulatory content is a business ruling (`[OPEN]`-8); until it is
made, it is a person's.

## Consequences

* A false confirmation removes a correct answer from search. That is why confirmation is a
  steward act over both texts, and why nothing a machine produced is ever served directly.
* The existing corpus is handled by the same funnel run as a batch, landing everything as
  `proposed`. The queue is ranked by how often the older clause has actually been retrieved —
  `audit_log.object_ref` already records every chunk id returned by every query (INV-11), so
  "which stale clauses are we serving, and how often" is answerable before any of this is built,
  and is the honest way to size the problem.
* A rechunk that renames a section path orphans rows. They are listed, not deleted — the
  pending-references lesson from ADR-0028: a record that has lost its target is a steward's
  problem, not a row to drop.
* The subject key and the quantity extractor are new denormalized chunk data, so they arrive on
  the same migration and rechunk as ADR-0032's `article` column rather than a second pass over
  the corpus.
* Quality is measured by two numbers in `eval/`: **stale-answer rate**, the share of answers
  citing a clause a confirmed newer one replaces, which is what this exists to reduce; and
  **false-supersession rate** on the `different_scope` bucket, which is what must not get worse.
  Gate 3 is expected to carry most of the second number, and the eval reports the split between
  pairs it resolved and pairs the model did.
