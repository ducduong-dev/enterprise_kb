# ADR-0033 — A detected clause supersession only proposes

**Status:** proposed · **Date:** 2026-08-13 · **Amended:** 2026-08-17 six times — by a second
reading of graphiti-core at source, and by building gate 5, the funnel, the retrieval side, the
answer and the metrics. All six are recorded in sections at the end rather than folded above.

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

## Corrections from building gate 5

Two claims above did not survive contact with the code (`kb_registry.adjudicate`, 2026-08-17).
Both are recorded rather than quietly fixed, because each was a plausible sentence that would
have produced the wrong behaviour.

**A verdict that contradicts the dates is not dropped — it becomes `conflicting_unresolved`.**
The Graphiti section above said "dropped with a log line rather than stored", and building it
made that look like exactly the failure this ADR exists to prevent. The prompt is not shown the
effective dates, so a model nominating the *older* clause as the replacement is not noise: it is
an independent reading that disagrees with the dates, on a pair the model has already said states
one rule. Dropping it discards a real finding silently, which is ADR-0028's failure mode wearing
a cautious face. It goes to a person instead, carrying both the model's own rationale and the
note that the two disagreed. What the original sentence was protecting is untouched and is the
part that mattered: **no `superseded` row is ever written against the dates.**

The same treatment covers the case the ADR did specify — direction `undecidable`, from an undated
pair or a same-day pair of equal rank — so there is one route out of gate 5 for "the model judged
this a supersession and the dates would not confirm which way", and it is a person's queue.

**`clause_supersessions` can hold only one of the four buckets.** The Storage section describes
the row as carrying a `verdict`, which reads as though all four live there. They cannot: the
table answers "what replaced this clause", `uq_clause_sup_open` allows one open row per replaced
clause, and a `different_scope` verdict stored with a null replacement is indistinguishable from
a pure abrogation — the opposite of what it means. A clause can also be `different_scope` against
one candidate and `superseded` by another, which the unique index forbids outright.

So gate 5 returns a verdict and writes nothing, and only `SUPERSEDED` maps onto `propose()`.

**Answered by `clause_pair_verdicts` (migration 0012), built with the funnel.** This table
records the *adjudication*; `clause_supersessions` records the *consequence*, and only one of
the four buckets has one. Four things about its shape are decisions rather than details:

* **Recording is the requirement, resumability is the bonus.** The AC says a same-subject pair
  differing only in customer segment is *recorded* `different_scope`, and this is where. The
  cost argument for the table was overstated when this section was first written: ADR-0035's
  cache already makes a repeated model call free, so what a stored verdict saves is the pair
  assembly and the gates, not the expensive part.
* **A row means we reached a conclusion.** Nothing is written for a pair gate 2 rejected (not a
  pair) or for one the model could not answer about (unreachable, unreadable reply). No row
  therefore means "never looked at, or looked at and did not conclude", and both must be
  retried — the whole retry rule, with no state column to keep in step.
* **The pair is normalized, not directional.** Stored with the lexically smaller
  `(document_id, section_path)` first and enforced by a check constraint, so adjudicating A
  against B and later B against A is one row. Direction lives in `clause_supersessions` where
  it means something; here it would give one pair two identities and let a backfill do the work
  twice.
* **`text_digest` is what keeps a stored verdict honest**, hashing both clause texts and the
  prompt version. A reworded clause no longer matches and is adjudicated again. Same rule as
  ADR-0035's cache key and for the same reason: an id survives an edit that changes the answer,
  and the text does not.

## Corrections from building the funnel

Two more, from wiring the gates together (`kb_registry.funnel`, 2026-08-17).

**The three paths are per document *pair*, not per document.** The Decision section reads as
though a document is on Path A, B or C. It is not: a circular routinely has an `amends` edge
naming articles to one instrument, an edge with no article list to a second, and no edge at all
to a third, and all three neighbours are in scope of the same run. So the funnel resolves the
path per neighbour — skipping Path A neighbours entirely, searching Path B neighbours with the
relaxed subject gate, and filtering both out of the corpus-wide Path C sweep so a pair is never
formed twice by two routes.

**Path B's basis is `detected`, not `edge_article`.** The Storage section lists `edge_article`
as a basis and it is tempting to give it to Path B, since an edge is what put the pair in scope.
That would overstate the evidence on exactly the rows a steward is deciding. `edge_article`
means *the corpus named the articles* — that is Path A, where nothing is detected and no row is
written here at all. On Path B the edge said only that two documents are related; which clause
replaced which is still our inference, adjudicated by the same model on the same four buckets as
Path C. The path is recorded on the review task, where it is useful context, rather than in
`basis`, where it would be a claim about provenance that is not true.

One more thing the funnel's tests caught, in gate 5 rather than in the funnel. The quantity delta
was computed in the order the pair was passed, so a run that happened to start at the newer
document produced `10%/năm → 8%/năm` — a steward's queue reading every change backwards, and the
same rule appearing to move in opposite directions depending only on which document a backfill
reached first. The delta is now recomputed old→new once direction is known, which is the first
moment it *can* be: gate 5 is deliberately not told which clause is older.

## Corrections from building the retrieval side

One, and it is an access decision that the Behaviour section got wrong by omission.

**The pointer has two halves and they obey different rules.** ADR-0033 says a confirmed
supersession "flags the older chunk *with a pointer*: `RetrievedChunk` gains `superseded_by`, so
the answer can say *Điều 8.2 đã được thay thế bởi Điều 5 A*". Built literally, that sentence
names another document to whoever retrieved the older clause — including a reader the filter
would never have shown the replacement to. `compile_sql_graph` refuses exactly this for an edge,
in as many words: *"what matters here is that the existence of the target is not disclosed to
someone who may not see it"* (INV-10). `[OPEN]`-3 leaves existence disclosure defaulting to off.

So `SupersededBy` splits. **That** a clause was replaced, and on what date, is a fact about the
clause the caller is already reading; it names nothing and is returned unconditionally, because
withholding it leaves someone acting on a stale figure with no reason to doubt it. **What**
replaced it — document, clause, title, citation label — is fetched through the caller's own
`compile_sql` filter, and is absent in every field when they may not read it. `names_replacement`
is the property an answer branches on.

The same query decides a case that is not about access at all: a replacement whose chunk has been
rechunked away resolves to nothing and is also left unnamed. Its title could be recovered from
`documents` — but only by hand-writing a second ACL predicate over that table, which is the
thing `compile_sql_graph`'s docstring warns produces leaks. One audited predicate that
occasionally says less is worth more than two that can disagree.

Two smaller notes from the same work. The drop runs on the **fused candidates**, before rerank,
so the freed slot goes to the next-best passage rather than shortening the answer — which also
means `top_k` cannot produce the "replacement not retrieved" case, since a replacement retrieved
at all removes its predecessor however short the answer is. And every dropped passage is named in
the audit record: it is invisible in the response by design, and "why was this clause not cited"
is exactly the question asked months later. Unlike the ACL's silent counter (ADR-0023) there is
nothing to conceal — the caller could have read the clause on its document page — so it is
recorded in full rather than counted.

## Corrections from building the answer

**The prompt rule is the improvement; the citation is the guarantee.** ADR-0033 says the pointer
exists "so the answer can say *Điều 8.2 đã được thay thế bởi Điều 5 A* rather than raising a
generic banner", which reads as though the sentence is the deliverable. It is not, and building
it that way would have been a mistake: a model that ignores the rule produces an answer quoting
a superseded figure as current, and nothing downstream would know. So `Citation` carries
`superseded_by` and `ChatService` raises `REPLACED_CLAUSE_WARNING` from the *citation* rather
than from the answer text. The reader is warned whether or not the model cooperated; the prompt
rule is what makes the warning unnecessary in the good case.

That warning is deliberately a second one rather than a reuse of M5's. "This document has an
unconsolidated amendment somewhere in it" and "the clause you are reading was replaced" are
different claims of different strength, and collapsing them would make the stronger one
invisible inside a sentence stewards already skim past.

**Both halves of `SupersededBy` reach the prompt, and the unnamed half carries an instruction.**
Where the caller may not read the replacement the context note gives the date and then says, in
the note itself, not to name or describe it. A note that merely omitted the name would invite a
model to fill the gap from the question or from its own weights, which is the one way this
feature could turn an access control into a disclosure.

**The replacement is never a numbered passage.** Fusion drops a superseded clause whenever its
replacement was retrieved, so a clause that reaches the context carrying this note is precisely
one whose replacement is absent from it. Both prompts therefore forbid assigning it a `[n]`
marker, and a test pins every bracketed label `render()` can emit to a rule in both prompt files
— because deleting a rule breaks nothing visible: the marker still appears, the model quietly
stops acting on it, and a stale rate is served with no warning by a build that is entirely green.

## Corrections from building the metrics

**The four numbers are a report, not a gate — and that is the finding, not a shortcut.** The
Consequences section says quality "is measured by two numbers in `eval/`", which reads like the
retrieval harness, where recall below 0.85 blocks a merge. It cannot be that yet. A threshold
for stale-answer rate or false-supersession rate has to come from a corpus with real confirmed
supersessions and real traffic, and setting one now would be inventing the finding rather than
measuring it. What is gated instead is the report's *honesty*.

That turned out to be the substance of the work. Every one of the four is a ratio counting a bad
thing, and on this corpus every one can be 0/0 — so a `Measurement` carries its numerator, its
denominator and what the denominator counts, and `rate` returns `None` rather than `0.0` when
there was nothing to divide. A spurious zero here would read as a clean bill of health for a
measurement that never ran, on precisely the corpus where nothing has been measured. Two further
places needed the same care:

* a stale-answer rate of 0% over 44 answers is a real measurement and a vacuous one when the
  corpus holds no confirmed supersessions, so the report says which it is;
* a gate/model split of 100% model looks like a gate regression when it is a corpus that states
  no scope facets for gate 3 to bite on, so the report says that too.

Three definitional choices are worth not re-deriving. **Refusals are outside the stale-answer
denominator**, because leaving them in would let the rate fall by refusing more — the wrong
incentive to build into the number that decides whether this milestone worked. **Undecided
proposals are outside the false-supersession denominator**, because counting `proposed` as "not
yet wrong" would make precision improve every time the backfill ran. And **the declared share
counts confirmed rows only**, because counting the funnel's unreviewed output would let the
inferred share rise by running the backfill again — measuring our own activity rather than the
corpus's drafting habits.

As of 2026-08-17 the report runs and three of the four say *not measured*. That is the honest
state of M9d: the code is complete and the corpus cannot yet grade it.
