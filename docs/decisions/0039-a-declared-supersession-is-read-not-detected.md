# ADR-0039 — A declared supersession is read, not detected

**Status:** proposed · **Date:** 2026-08-16

## Context

ADR-0033 designs clause-level supersession as an inference problem: two documents state the same
rule, neither cites the other, and a funnel of four gates plus a model decides whether one
replaced the other. That machinery is right, and it is aimed at the wrong majority.

In this corpus the replacing instrument nearly always **says so, in the text, in a sentence
written to be read**. Vietnamese drafting practice concentrates it in the closing article —
*Điều khoản thi hành* / *Hiệu lực thi hành* — in a small set of formulaic shapes:

* *"Thông tư này thay thế Thông tư 10/2022/TT-NHNN ngày 15/3/2022."* — whole instrument.
* *"Bãi bỏ khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN."* — one clause ends, nothing replaces it.
* *"Điều 5 Thông tư 10/2022/TT-NHNN được sửa đổi, bổ sung như sau: …"* — one article replaced,
  and the replacing text follows immediately.
* *"Điều 7 Thông tư này thay thế Điều 5 Thông tư 10/2022/TT-NHNN."* — both ends named, with the
  direction stated.

The business estimate is that around four in five of the supersessions in this corpus are
declared like this, and the rest are genuinely implicit — a rule re-stated in a later instrument
that never mentions the earlier one. The eval measures the real split; the design has to be
built for the first number, not the second.

Today the platform reaches these declarations only glancingly. `guess_ref_type` reads a cue
window before a mention and returns `abrogates` or `amends`, which is enough to raise a
document-level flag (ADR-0015) and nothing more. **No extractor anywhere populates
`document_refs.articles`** — the column exists in the ORM, the impact traversal reads it, and it
is filled by a human on the review screen or not at all. So the most common case in the corpus
arrives as an edge with no article list, which ADR-0033 files under Path B and describes as *"an
amendment whose article list failed to parse"*. It is not an exception. It is the norm, and it is
being handled by the exception branch.

## Decision

A **declaration extractor** runs at ingest, beside the date and legal-number extractors in
`kb_vntext`, and the declared path is the first path in the programme rather than a special case
of the inference funnel.

```python
# kb_vntext/supersession.py
@dataclass(frozen=True, slots=True)
class Declaration:
    kind: Literal["abrogates", "replaces", "amends"]
    target: LegalNumber | None  # None = the declaring document itself
    target_anchors: list[str]  # "12", "12.2", "12.2a"; empty = the whole instrument
    replacement_anchors: list[str]  # in the declaring document; empty for a pure abrogation
    effective_from: date | None  # when the declaration states its own date
    evidence: str  # the sentence, stored with the proposal
    span: tuple[int, int]
    confidence: float


def find_declarations(text: str) -> list[Declaration]: ...
```

**Patterns only, ADR-0013's terms.** The forms above are formulaic — that is exactly what makes
them extractable — so the pattern set is the floor and the model may add to it, never remove from
it. A shape the regexes miss is a fixture to add, not a threshold to tune.

**It resolves two things a similarity score never could.** *Direction*: the sentence names both
ends and which one replaced which, so nothing has to be inferred from dates or publish order, and
ADR-0033's careful fallback ladder (effective date, then instrument rank, then a person) is not
needed on this path at all. *Granularity*: *bãi bỏ khoản 2 Điều 12* ends one clause and leaves
the other three standing. Both ends are emitted in `build_citation_label`'s dotted form, so they
resolve to chunks by ADR-0036's equality join, with no threshold anywhere in the chain.

**A declaration whose target is not yet in the corpus is parked, not dropped.** Same machinery
and the same reasoning as the pending reference in ADR-0028: an archive digitised in whatever
order it yields will frequently produce the amending instrument first. The pending declaration
resolves when its target is ingested.

**A declaration whose target is the declaring document is the merge flow's business**, not this
one. An instrument that renumbers its own articles is a version diff (M5), and routing it here
would produce a document that supersedes itself.

**The same pass writes the edge's anchors.** ADR-0036 needs a re-detection pass over stored
KBDocs to populate `document_refs.anchors`; this is that pass. Reading the corpus twice for two
consumers costs twice and, worse, produces two answers that can disagree.

**Recall is the number it is measured on**, against a fixture set of real closing articles across
instrument types and both languages. A missed declaration does not vanish — it falls through to
the inference funnel, where it costs a model call and arrives with weaker evidence — but the
whole value of this path is that it is cheap and certain, and a recall number is the only thing
that says whether it still is.

## Consequences

* **The programme reorders.** The declared path is built and shipped on its own: no model in the
  critical path, no embeddings, no thresholds, and it covers most of the problem. ADR-0033's
  funnel then runs only over the pairs the declared path did not account for, which shrinks its
  candidate space and makes its false-positive budget affordable. Building the funnel first would
  have meant paying the hardest price for the smallest share.
* Confirmation gets much cheaper, and changes shape. One closing article typically declares a
  dozen changes at once, all read from the same paragraph, so the review screen confirms them
  **as a batch per declaring document** with the sentence shown once — not one pair at a time
  like an inferred proposal.
* `KBDoc` gains a `declarations` array beside `detected_refs`, so the extraction is visible in the
  IDP report and correctable on the review screen where a human already checks references.
* The residue is the model's place: sentences the patterns matched partially, and free-form
  drafting that does not follow the template. It proposes, exactly as everywhere else.
* Letting a model read the closing article instead was the tempting shortcut — it is one paragraph
  and a model would read it well. But this text is the authority for withdrawing a rule from
  service (ADR-0040). A pattern's failure is a miss, visible as an unconfirmed clause and caught
  by the funnel behind it; a model's failure is a fluent mis-parse of which side replaced which,
  arriving with the same confidence as a correct one. ADR-0013 already settled which of those the
  platform accepts as its floor.
