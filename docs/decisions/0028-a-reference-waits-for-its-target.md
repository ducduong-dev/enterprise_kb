# ADR-0028 — A reference waits for its target

**Status:** accepted · **Date:** 2026-08-12

## Context

References are detected when the *citing* document is parsed, and turned into edges by
matching each legal number against the registry. A number that matches nothing was reported
once, on the review task, and then dropped.

That makes the graph a function of ingest order. Nghị định 309/2026 amends 118/2025; ingested
in that order, the `amends` edge is never created, and ingesting 118/2025 afterwards does not
create it either — nothing revisits the earlier document. The corpus is being digitised in
whatever order the archive yields, so this is the common case rather than a corner.

The consequence is not a missing line in a diagram. `amends` is what raises the supersession
warning on the older instrument, what opens the consolidation task, and what the impact
traversal walks to find the internal policies that must change. A missing edge is **silence**:
the platform behaves exactly as if the amendment did not exist, and nothing in the system says
otherwise.

## Decision

A reference whose target is not in the registry is **parked**, not discarded, in
`pending_document_refs`: the citing document, the target's legal number (raw and normalized),
the reference type, the articles, and who detected it.

The moment a document with that legal number is created — from the ingest workflow, the
registry API or the seeder, since all three go through `RegistryService.create_document` — the
parked rows naming it become real edges and are deleted. The graph therefore converges on the
same shape whatever order the corpus arrives in, which is the property that was missing.

Three details that matter:

**A pending reference is not a half-edge.** It lives in its own table rather than as a
`document_refs` row with a NULL endpoint, because a NULL endpoint would oblige every consumer
of the graph — expansion, impact traversal, the review screen — to remember to exclude it.
INV-10 says edges target documents; a row that targets a string is not an edge.

**The promoted edge is unconfirmed.** It carries `detected_by` from the original detection and
`confirmed_by = NULL`, exactly as if both documents had been present from the start. A machine
match is a machine match whenever it happens.

**The serving projection is refreshed on promotion.** `graph_serving` is rebuilt by the publish
transaction of the document being published; these edges attach to documents published long
ago, so their copy is refreshed at promotion time or graph expansion never sees them. The
rebuild now has one implementation (`repository.refresh_graph_serving_for`), called from both
places.

## Why not re-scan instead

The alternative was to re-run reference detection over the whole corpus whenever a document
arrives, which needs no new table. It re-parses every stored KBDoc to answer a question the
system already knew the answer to at ingest time, and its cost grows with the corpus while the
work is proportional to one document. Parking the reference is the cheaper and the more honest
record: it says *this document said this*, at the time it said it.

## Consequences

* "What is this corpus missing?" becomes a query. The inspection screen shows a document's
  pending references, and the same table answers the backfill question across the corpus —
  which instruments are referenced but absent, ranked by how often.
* A legal number that is wrong (an OCR error, a typo in the source) waits forever, and looks
  identical to a document that has not been digitised yet. Neither is harmful — nothing acts
  on a pending row — but the list needs a steward's eye, which is why it is on the screen
  rather than in a log.
* Promotion happens at *registration*, not publication, matching the pre-existing behaviour
  for the other direction: an edge exists as soon as both documents are in the registry, and
  the edge carries the target's status so a reviewer can see it is still a draft.
* Renaming a document's legal number after creation does not re-resolve. That path is rare and
  guarded (it is an identity correction), and it would be a second trigger for the same rule;
  worth adding if identity corrections turn out to be common.
