# ADR-0030 — Expiry is a ledger entry, not a version column

**Status:** proposed · **Date:** 2026-08-13

## Context

`document_versions.effective_to` and its denormalized copy on `chunks` have existed since the
initial schema, and the ACL predicate has always read them: `kb_authz.compile` emits
`effective_to IS NULL OR effective_to >= :effective_on` on every read path. Set the column and
the chunk stops being retrievable that day, inside the query, with no post-filtering.

Nothing writes it. The only writer in the tree is the structured rate schedule, where a
publisher types an end date into a form (`rates.py` → `RegistryService.create_version` →
the publish transaction). No parser, no workflow, no edge has ever set it. ADR-0029 said so
explicitly and deferred the question: *"`effective_to` is still unset by ingest. It is not
stated in a document's own text — it arrives when a later instrument abrogates this one — so
it belongs to the consolidation flow, not here."* The consolidation flow never picked it up.

So the serving half of expiry is built and the deciding half does not exist. An instrument
that ceased to apply is served as current, forever, and the platform has no way to say
otherwise.

The obvious fix — start writing `effective_to` on the version — runs straight into INV-9.
`effective_from` could live on the version because the document states it *about itself*,
before publication; that is why `set_effective_from` refuses a version that is already
published (ADR-0029), on the grounds that the chunks carry a copy and moving the dates under a
published version changes what a past query would have returned. Expiry is the opposite shape:
it is almost always learned *later*, from a *different* instrument. A rule that can only be
recorded before publication cannot record it at all.

## Decision

Expiry is a decision about a document's lifecycle, so it is written down as a decision, in its
own append-only table, and never by mutating a published version.

```sql
document_expiry(id UUID PK, document_id UUID REFERENCES documents,
  effective_to DATE,
  basis TEXT CHECK (basis IN ('self_stated','abrogated_by','steward')),
  source_document_id UUID REFERENCES documents,   -- the instrument that ended it, when there is one
  articles INT[],                                  -- empty/NULL = the whole document
  evidence TEXT,                                   -- the sentence it was read from
  state TEXT CHECK (state IN ('proposed','confirmed','revoked')),
  detected_by TEXT, decided_by TEXT, decided_at TIMESTAMPTZ, created_at TIMESTAMPTZ,
  closed_at TIMESTAMPTZ);                          -- when a later row replaced this one
```

The date retrieval evaluates is `coalesce(<the open confirmed ledger row>, version.effective_to)`.

Six properties this is built around:

**Nothing is ever updated.** A withdrawn expiry is a new `revoked` row, not a deleted one. "Why
did this instrument disappear from search on 3 May, and who decided that?" is a question an
auditor will ask, and it must be answerable from one table without reading a log.

**Two clocks, kept apart.** `effective_to` is when the instrument stopped applying *in the
world*; `created_at` and `closed_at` are when this platform started and stopped *believing* it.
Without the second pair the table answers "what was in force on 3 May" but not "what would this
platform have answered on 3 May" — and when an expiry is discovered late, or a date is
corrected, those give different answers. The second is the one an auditor reconstructing a
past answer is actually asking, and INV-11 already promises it for retrieval. A row is closed by
the row that replaces it, in the same transaction; the open row is the current belief.

This is borrowed. Graphiti (`graphiti-core`, evaluated in `eval/graphrag/protocol.md`) keeps
exactly this split on every edge — `valid_at`/`invalid_at` for the world, `created_at`/`expired_at`
for the system — and arrives independently at the same append-only, invalidate-never-delete
shape. Its automatic invalidation is what we do not take (ADR-0033).

**The version column keeps its meaning.** A fee schedule that says on its face it applies until
31/12 is stating a version fact, known at publication, and it stays on the version. The ledger
is for expiry that arrives afterwards. When both exist the ledger wins — and the inspection
screen must show both, or a steward who set a date on the version will not understand why a
different one is in force.

**The evidence travels with the decision.** Same discipline as ADR-0029's review screen: the
sentence the date was read from is stored, so confirming it is one glance rather than a
document read.

**Articles are part of the record.** Vietnamese practice abrogates in pieces — *"bãi bỏ Điều 5
Thông tư 10/2022"* — and the `abrogates` edge already carries the article list. A ledger row
with articles is not a full expiry; it is a partial one, and it hands off to ADR-0032/0033
rather than taking the document out of service.

**Only `confirmed` is served.** A detector writes `proposed`. For `regulatory` and
`customer_facing` documents a human confirms, because withdrawing what the bank tells people
changes what the bank tells people as surely as publishing does, and INV-8 exists for exactly
that. Whether the other two classes auto-confirm is a business ruling, recorded as `[OPEN]`-6.

## Why not the alternatives

**Relax INV-9 for this one column.** The invariant's value is that it has no exceptions; the
first one makes "versions are immutable" something you have to check rather than something you
know. And the practical objection stands: the chunks carry a copy, so a version whose dates
move under it makes a past `as_of` query irreproducible.

**Publish a new version whose only change is the date.** It obeys INV-9 and costs a full
chunk-and-embed cycle to record a fact about text that did not change. Worse, it is a lie in
the version history: a version is the bank's record of *what a document said*, and one whose
text is byte-identical to its predecessor tells an auditor that something was re-issued when
nothing was.

## Consequences

* Retrieval's effectivity predicate is unchanged. What changes is that the chunk copies now get
  written — at confirmation time, not at expiry time (ADR-0031).
* A document accumulates rows over its life: proposed, confirmed, sometimes revoked and
  re-proposed with a corrected date. The inspection screen shows the sequence, which is the
  point.
* "What did we believe on date D" is a query — `created_at <= D AND (closed_at IS NULL OR
  closed_at > D)` — and belongs on the same screen as `as_of`, because a reviewer investigating
  a past answer needs both clocks and will otherwise reach for the wrong one.
* `documents.status` and the ledger can disagree between confirmation and the date arriving —
  a document confirmed today to expire in December is `published` with a confirmed row. That is
  correct, and the status flip is ADR-0031's job.
* Nothing here backfills the corpus. As in ADR-0029, a date nobody read is what this ADR exists
  to avoid; the detectors propose and the queue is worked.
