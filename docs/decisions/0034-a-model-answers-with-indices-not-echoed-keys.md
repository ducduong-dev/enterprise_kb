# ADR-0034 — A model answers with indices, not echoed keys

**Status:** accepted · **Date:** 2026-08-13 · **Implemented:** 2026-08-16 (`MergeDrafter._classify`,
`merge_classifier.md` v2). ADR-0033's clause adjudicator inherits the rule when it is built.

## Context

Where the platform asks a model about a *list* of things, it currently asks the model to name
each one back. `MergeDrafter._classify` sends the changed sections and reads the reply into
`by_path = {item["section_path"]: item}`, then looks each change up by its own path.

Three different failures land in the same place, and only one of them is visible:

* **The model skipped a section.** Handled: `_from_diff` stands in, flagged `inferred=True`, so
  the reviewer sees that this one was not classified.
* **The model altered the path.** `Điều 12 > Khoản 2` comes back as `Điều 12, khoản 2`, or with
  a diacritic dropped — which is likely, since these paths come out of OCR and the model is
  retyping them. The lookup misses, the section falls back to `_from_diff`, and a classification
  the model actually produced is thrown away.
* **The model invented a path.** The item sits in `by_path` matching nothing and is silently
  ignored.

The last one also corrupts the honesty signal. Completeness is computed as
`len(by_path) >= len(changes)` — so a reply that names four real sections and one imaginary one,
while omitting a real one, counts as five and reports `complete=True`. `MergeDraft.complete`
exists precisely so the merge screen can tell a reviewer the draft is partial, and in that case
it says the opposite.

The cause is that the *key space* is open. The model is free to emit any string, so the code has
to guess what it meant.

## Decision

A model call over a list receives the items numbered, and answers with indices into that list.

```python
class SectionVerdicts(BaseModel):
    verdicts: list[SectionVerdict]  # each carries `idx: int`


# the caller, not the model, owns the mapping
if not 0 <= verdict.idx < len(changes):
    ...  # out of range: the call failed, it did not partly succeed
```

Three rules make it worth the change:

**Every returned index is range-checked, and an out-of-range index fails the call.** It is not
dropped and it is not repaired. A model that answers about item 7 of a 5-item list did not
understand the question, and the other four answers are not evidence that it did.

**Every input index must be answered exactly once.** A missing index is a detected omission —
`_from_diff` still stands in, but now because we *know* it was omitted. A duplicated index is a
failed call. Completeness stops being an inference.

**The model never retypes a section path.** The paths still appear in the prompt, as labels for
the reader; they are just not the channel the answer travels on. Nothing about an OCR'd heading
can break the mapping any more.

This is borrowed from Graphiti's `EdgeDuplicate`, which returns `duplicate_facts: list[int]` and
`contradicted_facts: list[int]` over a numbered fact list and validates the range before use
(`eval/graphrag/protocol.md`).

Applies to `MergeDrafter._classify` today and to ADR-0033's clause adjudicator when it is built;
it is the default for any list-shaped model call the platform adds.

## Why not fuzzy-match the echoed path

It works, and it is a second matcher to own — thresholds, diacritic folding, tests — solving a
problem we created by leaving the key space open. It also papers over exactly the case worth
seeing: a model that cannot keep a path straight over five sections is a model whose
classifications deserve a closer look.

## Consequences

* `MergeDraft.complete` becomes trustworthy, which is the whole reason it exists.
* Prompts get shorter and cheaper: the reply carries an integer instead of a re-typed path.
* This changes `merge_classifier.md`, so `PROMPT_VERSION` moves and the prompt's eval cases are
  re-run. That is the cost, and it is the ordinary cost of a prompt change.
* A model too weak to hold index alignment over a long list will now fail loudly instead of
  degrading quietly. That is the intent; if it happens routinely, the answer is to batch the
  list into smaller calls, not to go back to matching strings.
