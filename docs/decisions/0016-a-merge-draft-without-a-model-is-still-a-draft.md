# ADR-0016 — A merge draft without a model is still a draft

**Status:** accepted · **Date:** 2026-08-11

## Context

`build_merge_draft` calls a model twice: once to classify each changed section into the three
buckets the plan names, once per section to draft the consolidated text. Both calls can fail —
the GPU node is busy, the endpoint is down, the model returns something that is not JSON.

The tempting behaviours are to retry until the activity gives up (the review never opens, and
the consolidation is invisible), or to publish the mechanical result as though a model had
produced it (the reviewer trusts prose that nobody wrote).

## Decision

Model failure degrades the draft; it never blocks the merge.

* Every changed section still appears, classified from the **diff's own verdict**: an edited
  article is `amended`, an added or removed one is `new_or_abrogated`. The one bucket the
  fallback never guesses is `unchanged_in_substance` — that is the guess that loses
  information, because a reviewer who is told nothing changed stops reading.
* Each such classification carries `inferred=True`, and the draft carries `complete=False`.
  Both reach the merge screen, which marks the rows and says the draft is incomplete.
* A section the drafter could not write shows the incoming text with `drafted=False` rather
  than an empty pane.

In CI there is no model at all: `Deps.generation` is `None` and the drafter is handed a port
that always fails, so the fallback path is what the test suite exercises by default. The
model path is covered separately with `ScriptedGeneration`.

## Consequences

* A consolidation can always start. What varies is how much writing the Legal cell has to do.
* "The model said this" and "the diff implies this" are distinguishable everywhere they are
  shown — in the payload, on the screen, and in the review task.
* An incomplete draft cannot be silently approved into existence: the approval still needs the
  same two people (INV-8), and they are looking at a screen that says it is incomplete.
