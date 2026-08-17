# ADR-0037 — An answer covers the fact, not the best passage

**Status:** proposed · **Date:** 2026-08-16

## Context

The funnel is a ranking. `reciprocal_rank_fusion` orders by rank agreement between the two
retrievers, `cap_per_document` keeps at most `MAX_CHUNKS_PER_DOCUMENT = 3` passages from any one
document, the reranker reorders the survivors, and `top_k` truncates. Every part of that answers
one question well: *which passages read most like this question.*

Nothing in it answers the question a reader is actually asking: *what does the bank say about
this.* In this corpus a rule is habitually stated in several places at once — the circular states
it, the internal normative document implements it, an operational procedure restates it with the
number filled in, and the fee schedule carries the figure. Those four sentences are phrased
differently on purpose, by different departments, years apart. Similarity to the question is
exactly the axis on which they differ.

So the fourth document ranks fourteenth and is not in the answer. `cap_per_document` bounds how
much of one document can crowd in; nothing raises how many documents get in at all, and the
comment on that function — *"the answer is built from a single source, which reads as confident
and is exactly when it is most likely wrong"* — describes the failure it half-fixes. The
chatbot then answers from three sources with the fluency of having read everything, and nothing
in the response says a fourth existed. ADR-0028's failure mode: silence indistinguishable from
correctness.

It also blocks the work downstream. The whole premise of clause-level supersession (ADR-0033) is
that two documents state the same rule differently. A detector cannot warn about a conflict the
funnel never surfaced, and a reader cannot see one either.

## Decision

Retrieval answers with **fact sets**. After rerank, a coverage pass takes the top-ranked passages
as seeds and pulls in the other passages that state the same fact, and the answer is built from
the union.

Membership comes from three channels, in descending order of confidence:

**1 · The corpus said so.** A clause that cites the seed clause, or is cited by it, resolved
through ADR-0036's anchors. This is the strongest signal available and it costs an equality join:
an implementing procedure that names *khoản 2 Điều 12* is about that rule by the bank's own
statement, not by our inference.

**2 · The subject key matches.** ADR-0033's normalized subject key, once it exists — the heading
chain with boilerplate stripped, plus the item label for structured content. Exact, indexed, and
already being built for the detector.

**3 · A second embedding round, seeded by the passage rather than the question.** Cosine against
the seed's own vector above a threshold. This is the only channel that reaches a rule phrased in
words the question never used, which is the case the whole ADR exists for, and it is available
before channel 2 is built.

Five rules govern it:

**The coverage pass runs through the same filter.** It is a second query compiled by the same
`FilterBuilder` from the same principal, inside the index (INV-1, INV-2). A fact set is never a
reason to widen. This is stated explicitly because it is precisely where somebody writes "we
already checked the seed, just fetch its neighbours".

**A member the caller may not read is omitted and recorded.** Not disclosed — existence
disclosure is decided by the target's category as it is everywhere else (ADR-0023). But the
audit record says the set was narrowed by the filter, because a reviewer reconstructing why an
answer missed something needs to tell "we did not have it" from "they could not see it" (INV-11).

**Coverage before depth.** `assemble` walks a flat ranked list and drops the tail; that spends the
budget on the top document's third clause while the fourth document's first clause never appears.
Budget is allocated one passage per document across the fact set, then a second round, and so on.

**Truncation is stated.** `AssembledContext.dropped` already counts what fell out and nothing
reads it. An answer built from part of the evidence says so, and says how many documents it left
out — not which, since that would disclose by count what the filter withheld, so ACL narrowing
and budget truncation are counted separately and only the second is spoken.

**The set is capped, and the cap is a number.** "All related passages" is unbounded over 3,000
documents. What is bounded, and what this promises, is *every document that states the fact, up
to N, and a statement when there were more.* How large N can be before an answer stops being
readable is a product call (`[OPEN]`-12).

## Why not simply raise `top_k`

Because the list is not too short; it is ordered on the wrong axis. Raising `top_k` deepens the
same ranking, and the reranker fills the extra slots with near-duplicates from the documents
already present — which is why `cap_per_document` had to exist. The passage worth adding ranks
low *because* it words the rule differently, and no amount of depth in a likeness ranking
promotes it above a closer paraphrase of the question.

## Consequences

* Answers cite more documents and get longer. That is the requested behaviour, and the ceiling is
  a product decision rather than a technical one.
* **Contexts will now routinely contain two documents that disagree** — which is the point, and
  is also a new way to be wrong. Until M9 can say which clause replaced which, the prompt must
  require the answer to *present* the conflict: name each source's instrument and effective date
  whenever two passages in a set state different quantities, and never silently pick one. After
  M9, a confirmed supersession removes or labels the older passage and the answer stops having to
  hedge.
* One extra indexed lookup and at most one extra vector query per request, bounded by the seed
  count. It sits inside the same latency budget as graph expansion and is measured with it.
* The golden set needs re-labelling: relevance today is graded per query→passage, and coverage
  can only be measured if a fixture knows *which documents state the fact*. That is the real cost
  of this ADR, and it is work on the eval set rather than on the code.
* `eval/` gains **fact coverage** — of the documents that state the answer's rule, the share
  present in the context — reported beside recall@k, which it does not replace.

## Corrections from building it (M10, 2026-08-17)

**Channel 2 could not fire at all, and the reason was upstream.** `chunks.subject_key` was NULL
for every chunk in the corpus — not a backfill that had not run, a derivation that could never
work: it was computed from `section_path`, which carries structural *labels* ("Điều 6") because
a label is a stable join key, while a subject key needs the heading's *title* ("Tỷ lệ an toàn
vốn"). Fixed in the chunker (`ad8b695`); ADR-0033's gate 1 was running on one channel for the
same reason, which is what "by_gate: 0" in the M9d backfill was really saying.

**Exact equality on the whole heading chain is narrower than this ADR implies, and the fixture
corpus shows it.** `subject_key` unions the tokens of *every* level of the path, so an ancestor
heading pollutes the key: the regulator's `Chương II. Tỷ lệ an toàn vốn > Điều 6. Tỷ lệ an toàn
vốn tối thiểu` and the internal policy's `Phần 2. Quản lý vốn > Mục 3. Tỷ lệ an toàn vốn tối
thiểu` state the same rule under the same leaf title and produce different keys, because
"Quản lý vốn" is in one chain and not the other.

The consequence is that channel 2 fires between documents of the same structural shape — in
practice, two versions of one instrument — which is the case that least needs a fact set. The
cross-instrument case this ADR was written for falls to channels 1 and 3. In the seeded corpus
the capital fact is covered by **reference**, not by subject, and coverage still reaches 1.00.

This is recorded rather than fixed because the fix is a change to a field two milestones share:
keying on the *leaf* title alone would make the channel work as described here and would also
change ADR-0033 gate 1's candidate set, which is a detector decision with its own false-positive
budget. It wants measuring before it is changed, and the measurement now exists — the eval
reports coverage per entry and the funnel reports its gate/model split, so the claim "the leaf
is the subject and the ancestors are context" can be tested rather than asserted.

**Fact coverage is `None` where a fact is unlabelled, never 1.0.** `recall_at_k` scores an empty
expectation 1.0 and is right to — "the correct answer is nothing" is a real ACL expectation.
Here the same convention would flatter: a rule stated once is trivially covered, so counting
unlabelled entries would let the headline number be raised by adding single-source queries
rather than by covering anything. The runner prints `facts_measured` beside the rate and
declines to gate when it is zero.
