# ADR-0018 — An answer with no valid citation is a refusal

**Status:** accepted · **Date:** 2026-08-11

## Context

A grounded-answer prompt asks the model to answer only from the supplied passages and to cite
each claim. Models mostly comply. "Mostly" is not a control, and the failures are exactly the
ones that matter in a bank: a citation to `[7]` when six passages were supplied, a citation
attached to a sentence the passage says nothing about, a confident number the passage does not
contain, an answer with no citation at all.

Each of those reaches a compliance officer as a fluent Vietnamese paragraph with a legal
reference beside it. The reference is what makes it quotable, and the quote is what makes it
dangerous.

## Decision

Citations are verified against the passages before the answer is returned, and the answer's
right to exist depends on what survives:

* a marker with no passage behind it is **stripped from the text**;
* a citation whose sentence shares too little substantive vocabulary with its passage is
  **dropped** — the sentence keeps its words but loses its claim to authority;
* a citation whose sentence contains a **figure the passage does not** is dropped outright.
  Vocabulary overlap cannot tell "tối thiểu là 8%" from "tối thiểu là 12%"; in a bank the
  number *is* the claim, so the numbers are checked separately;
* an answer left with **no citations** is not returned at all. The surface's fixed refusal is,
  and the attempt is logged with what was pruned and why.

A marker is read as citing the sentence it appears in *or* the sentence it follows, because
both are ordinary citation style and rejecting one of them would fail correct answers on
punctuation.

## Consequences

* The bot's failure mode is silence, not invention. That is the right direction for a bank: a
  refusal sends someone to the document, a fabrication sends them to a customer.
* Refusals are not errors and are not treated as such — they are logged, counted, and graded
  in the answer set, where "answered a question it had no basis for" scores zero.
* None of this makes a model truthful. It makes an untruthful one visible, which is the only
  property that can be tested on every CI run.
