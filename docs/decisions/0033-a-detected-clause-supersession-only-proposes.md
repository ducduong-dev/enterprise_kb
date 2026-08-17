# ADR-0033 — A detected clause supersession only proposes

**Status:** proposed · **Date:** 2026-08-13 · **Amended:** 2026-08-17, by a second reading of
graphiti-core at source (see *What re-reading Graphiti's source changed* at the end).

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
guard (`graphiti_core/utils/maintenance/edge_operations.py:554-561`, `eval/graphrag/protocol.md`).
It lives as a pure function as well as a SQL predicate, because the interesting cases are boundary
dates and those should be testable without a database.

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
carries an instruction taken from Graphiti's `resolve_edge`
(`graphiti_core/prompts/dedupe_edges.py:53`): *never treat two clauses as the same rule restated
when they differ in a numeric value, a date, or a qualifier* — for a fee or a rate that is the
whole distinction, and a model reading for gist will call them identical. The adjudicator answers
with indices rather than echoed section paths (ADR-0034).

The same prompt's third example is worth porting too, translated to a clause pair: *"Bob ran 5
miles on Tuesday" / "Bob ran 3 miles on Wednesday" → neither duplicate nor contradiction*
(`dedupe_edges.py:94-96`). Same subject, same numeric shape, different applicability — the hardest
answer to get out of a model, and exactly the `different_scope` case gate 3 could not settle
because the text was not explicit. Its *encoding* is what we do not take: Graphiti expresses that
verdict as two empty lists, which is indistinguishable from a model that found nothing. Ours is a
named bucket, and that difference is the whole false-supersession measurement below.

**The model nominates; the dates dispose.** Graphiti separates these and it is the better shape:
`resolve_edge` returns only indices, and a pure date function then decides what is actually
invalidated (`edge_operations.py:841-844`). So the adjudicator's verdict is not written straight
into a row — `windows_overlap` and `older_first` are re-applied to the pair the model chose, and
a verdict that contradicts them is dropped with a log line rather than stored. It costs nothing,
and it makes a model that nominates a backwards pair unable to produce a backwards row.

The funnel's shape is the point. On NĐ 118/2025 — 208 chunks, measured — five candidates each
would be 1,040 pairs; gates 2 to 4 are meant to leave the model a small fraction of that, and how
small is the number the eval reports. If that number makes pairwise calls unaffordable, the
fallback is Graphiti's batched encoding — one clause against N candidates in a single call, two
lists sharing one continuous index space with the second offset by the length of the first
(`edge_operations.py:700-707`). Note before reaching for it that Graphiti's own caller has to
range-validate both lists separately and log out-of-range values it cannot trust
(`:735-744`, `:760-767`). That is evidence the encoding is error-prone, and an argument for
keeping pairs until the backfill's measured cost forces the change.

### Direction comes from the legal date, never from publish order

Which clause is the older one is decided by `effective_from`, falling back to the issue date.
**Never** by `created_at`, `published_at` or registry insertion order: the corpus is being
digitised in archive order, which is exactly ADR-0028's point, so publish order says nothing
about which rule came first. Where the effective dates are equal, the instrument's rank decides
(a Nghị định outranks a Quyết định), and where rank is equal too, nothing is proposed — that is a
`conflicting_unresolved` for a person.

### Detection is symmetric, because the older text often arrives second

The funnel runs on ingest, which makes it natural to write it as *"does this new clause supersede
something already indexed?"* — and that is wrong here for the same reason publish order is. The
archive is digitised in whatever order it yields, so a 2023 circular routinely lands after the
2026 one is already in the index, and the pair the funnel finds is a real supersession pointing
the other way. The run must therefore be able to propose the **already-indexed clause as the
replacement** and the arriving one as replaced, and a detection pass that can only ever produce
rows in one direction is broken in a way nothing else will report: it simply finds less.

Graphiti handles the same case explicitly. Before invalidating any candidate,
`resolve_extracted_edge` checks whether a candidate is *newer* than the edge being resolved, and
if so expires the **incoming** edge instead (`edge_operations.py:825-839`). It reaches the
answer by sorting candidates on `valid_at` and comparing, never by which arrived first — which is
this ADR's direction rule arriving at the same place from the same premise.

Concretely: `older_first` decides the roles after the pair is formed, not before, and the
backfill's acceptance test asserts that ingesting the 2023 and 2026 fixtures **in either order**
produces the identical proposed row.

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

When a flagged clause does reach the answer prompt, it carries `supersedes_from` and the
`superseded_by` pointer as **structured fields beside the passage**, never as a sentence prepended
to its text. Graphiti serialises facts to the model this way — `valid_at` and `invalid_at` per
fact, with the reading rule stated once in the surrounding instruction
(`graphiti_core/search/search_helpers.py:25-57`) — and it is the right shape for a passage the
model must cite *as* superseded rather than quietly quote. What we do not take is that this is
Graphiti's only temporal control; see below.

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

Reading that code closely adds an argument the framework evaluation did not make: **there is no
way back.** `expired_at` is set once, in the same transaction as ingest, and nothing in
graphiti-core ever clears it — an invalidation is not a state a reviewer can reverse, because it
is not a state at all. This ledger's `revoked` restores the clause, and the acceptance criteria
require it to. For a corpus where a steward can be wrong about which of two rules survived, an
irreversible automatic decision is the disqualifying property, more so than the missing review
step: a review step can be added to a design that can be undone.

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

## What re-reading Graphiti's source changed

Written 2026-08-13 from the framework evaluation, before gate 5 existed. graphiti-core 0.29.3 was
re-read at source on 2026-08-17 — the same version `eval/graphrag/protocol.md` measured — with the
narrower question of what its *temporal* machinery has that this design does not. The adoption
answer is unchanged and the gate in the protocol still decides it. Four things above are new, and
are recorded here rather than folded in silently because each changes something buildable:

1. **Detection is symmetric** (new subsection). The gap this closes was ours, not the protocol's:
   nothing in the 13 August text said which document the funnel runs *from*, and the obvious
   reading produces a one-directional detector on a corpus ingested in archive order.
2. **The model nominates, the dates dispose** (gate 5). A shape borrowed from the split between
   `resolve_edge` and `resolve_edge_contradictions`, not from either one of them.
3. **The abstention example** (gate 5), ported for its content and explicitly not for its
   encoding — Graphiti has no fourth bucket, and says "different scope" by saying nothing.
4. **Irreversibility** (why four buckets). The evaluation recorded that Graphiti's invalidation is
   automatic and unreviewable; what the source adds is that it is also unrevocable, which is the
   stronger objection and the one that survives someone proposing to bolt a review queue on.

One thing was checked and needed no change. Graphiti uses a half-open convention — the invalidated
edge's `invalid_at` is set to its successor's `valid_at` (`edge_operations.py:569`) — while the
declared path here uses closed intervals, `effective_to` being the last day the rule applied. Both
conventions coexist in this platform, but `DeclarationReview._dates` returns the pair from one
function with the boundary reasoning stated in its docstring, so the changeover day has one
answer. The detected path writes `supersedes_from` and no expiry, so only one end of that pair
applies to it — and it takes the date from the same helper rather than recomputing it.
