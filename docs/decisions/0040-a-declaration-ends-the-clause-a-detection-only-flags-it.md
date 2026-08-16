# ADR-0040 — A declaration ends the clause; a detection only flags it

**Status:** proposed · **Date:** 2026-08-16

## Context

Two mechanisms now produce clause-level supersession, and they carry evidence of completely
different kinds. ADR-0039's declaration is the corpus stating a fact about itself. ADR-0033's
detection is our reading of two texts that never mention each other.

ADR-0033 settled the behaviour for its own output, and settled it correctly: a confirmed
supersession **flags** the older chunk with a pointer, fusion drops it when the replacement is in
the candidate set, and it is never hidden — *"the clause is still in the document, and a page that
omits it lies about what the document says."*

That is right for an inference and too weak for a declaration. When the instrument says *"khoản 2
Điều 12 hết hiệu lực kể từ ngày 01/01/2026"*, that clause did not become less likely to be
current. It stopped being in force. Serving it on the default path with a banner attached is
serving a repealed rule and hoping the reader reads the banner — and for the external surface,
hoping a customer does.

The mechanism for this was built and stopped one step short. ADR-0030's ledger already carries
`articles`, and says of a row that has them: *"A ledger row with articles is not a full expiry; it
is a partial one, and it hands off to ADR-0032/0033 rather than taking the document out of
service."* Nothing catches the hand-off. ADR-0032 narrows a *warning* to an article; ADR-0033
flags a clause. Neither ends one. So the partially expired document — the ordinary shape in this
corpus, because Vietnamese practice abrogates in pieces — has no representation in serving at all.

## Decision

A confirmed declaration writes a **partial expiry** into the ledger, and a partial expiry is
projected onto the chunks its anchors resolve to.

**The ledger row gains `anchors TEXT[]` beside `articles`**, in the same dotted form as everywhere
else. `articles` stays and is derived from the anchors: the impact traversal asks a genuinely
article-shaped question and keeping both honest is the reason ADR-0036 gave for keeping both.
`document_expiry.basis` gains `'declared_by'` alongside ADR-0030's `self_stated` / `abrogated_by`
/ `steward`, and `clause_supersessions.basis` gains `'declared'` alongside ADR-0033's
`edge_article` / `detected` / `steward` — two enum values, no new tables. The basis is what the
serving rule below is decided on, so it is a stored fact rather than something re-derived from
which detector happened to write the row.

**Confirmation projects, in the same transaction.** The anchors resolve against the target's
canonical chunks by ADR-0036's equality join, and `effective_to` is written onto *those chunks
only* — exactly as ADR-0031 projects a full expiry, and for the same reason. Retrieval needs no
new code whatsoever: `kb_authz.compile` already emits `effective_to IS NULL OR effective_to >=
:effective_on`, so the clause leaves the default path on its date, remains reachable under
`as_of`, and none of that depends on a scheduled job having run.

**The ledger stores the last day the rule applied.** The predicate is inclusive, so a clause
replaced by an instrument effective 01/01/2026 is stored with `effective_to = 2025-12-31`. Stated
once here, and asserted by a boundary test rather than a comment — off-by-one on this date means
either a day of repealed rule served or a day of current rule hidden.

**The sweep does not touch a partially expired document.** `documents.status` stays `published`;
`expiry_sweep` skips ledger rows that carry anchors (ADR-0031 — the sweep applies, it never
decides). A document all of whose articles have been separately expired is a question for a
steward, not an inference for a nightly job.

**A replacement gets a pointer as well as an ending.** Where the declaration names one, the same
confirmation writes the `clause_supersessions` row with `basis='declared'`, so the refusal can
name what took over: *"khoản 2 Điều 12 TT 10/2022 hết hiệu lực từ 01/01/2026; quy định hiện hành
là Điều 7 TT 15/2025"* — ADR-0030's named refusal, now at clause granularity, and the reason it
reads as help rather than a dead end. A pure abrogation writes the ledger row and no pointer, and
the refusal names only the ending.

**A detected supersession never writes the ledger.** It keeps ADR-0033's behaviour unchanged:
flag, pointer, fusion drop, nothing hidden. The dividing line is not a confidence threshold that
a detection could one day cross — it is what kind of fact is on the record. A declaration is
testimony from the corpus about what the law now is; a detection is our conclusion about two
texts. Only the first is evidence that a rule *ended*, and no accumulation of the second turns
into the first.

Confirmation stays human for `regulatory` and `customer_facing` (INV-8, `[OPEN]`-6). What changes
is the cost of it: the screen shows the sentence, both clauses, and the resolved anchor, and one
closing article's dozen declarations are confirmed together (ADR-0039).

## Consequences

* Expiry becomes expressible at the granularity the corpus actually uses. A sixty-article circular
  can carry four dead clauses and fifty-six live ones, and answers stop quoting the four.
* **The failure direction is now hiding, and that is the risk to hold.** A wrongly confirmed
  declaration removes a live rule from every default answer — the mirror of ADR-0032's warning
  about under-warning, one step more consequential. Three things hold it: nothing machine-produced
  is ever served (the row is `proposed` until a person confirms it), anchor resolution is an
  equality join rather than a fuzzy match so a wrong anchor almost always resolves to *nothing*
  rather than to the wrong clause, and an anchor that resolves to nothing is reported to a steward
  (ADR-0036), never applied blindly.
* Revocation works because the ledger is append-only: a `revoked` row re-opens the clause by
  re-projecting nulls onto the same chunks, and the whole sequence is on the inspection screen
  where an auditor asking "why did this clause disappear in May" can read it.
* A rechunk after confirmation must re-project: the chunks are new rows and the ledger is the
  authority. `PublishService.rechunk` gains that step, which is also the test that catches anyone
  storing the projection as the source of truth.
* The eval gains **repealed-clause serve rate** — answers citing a clause a confirmed declaration
  ended — which should sit at zero and is a harder failure than ADR-0033's stale-answer rate,
  because the corpus told us and we served it anyway.
* This is what makes ADR-0037's coverage pass safe to widen. Pulling every document that states a
  rule into the context puts repealed clauses in front of the model far more often than a narrow
  top-k did; the two changes should not be a long way apart in the schedule.
