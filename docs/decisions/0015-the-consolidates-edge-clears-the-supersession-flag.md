# ADR-0015 — The `consolidates` edge is what clears the supersession flag

**Status:** accepted · **Date:** 2026-08-11

## Context

Since M2, retrieval flags a document as superseded when a published instrument `amends` or
`abrogates` it. M5 introduces the act that resolves that state: the Legal cell approves a
consolidated text (`văn bản hợp nhất`), and the amendment is folded in. Something has to tell
retrieval to stop warning.

The obvious candidates were a boolean column on `documents`, a status transition on the
amendment, or an event the indexer consumes. Each has the same failure mode: the flag and the
canonical text can disagree. A column set after the publish commits leaves a window where the
new text is served with a stale warning; an event leaves a window that lasts as long as the
consumer is behind.

## Decision

The consolidation writes a `consolidates` edge from the target to the amending instrument,
**inside the publish transaction**, and the supersession predicate excludes any amendment for
which that edge exists:

```sql
AND NOT EXISTS (
      SELECT 1 FROM document_refs c
      WHERE c.src_document_id = r.dst_document_id
        AND c.dst_document_id = r.src_document_id
        AND c.ref_type = 'consolidates'
)
```

There is no separate flag to keep in step. The same commit that makes the consolidated text
canonical is the one that stops the warning (INV-5), so no reader can observe one without the
other.

The predicate exists twice: `kb_registry.publish.SUPERSEDED_PREDICATE` for the single-document
question and `RetrievalEngine._superseded_documents` for the bulk one. That duplication is
deliberate — retrieval-api does not depend on kb-registry, and adding the dependency to share
six lines of SQL would couple the read path to the write path for no benefit. Drift is caught
by a test that asserts the two agree on the same corpus.

## Consequences

* The `amends` edge is never deleted or rewritten. The history of *why* the text changed
  survives consolidation (INV-9); only the warning goes away.
* Consolidating one of several amendments clears only that one. A second unconsolidated
  amendment keeps the document flagged, which is correct.
* An operator can un-consolidate by deleting the edge — visible, auditable, and reversible,
  unlike a column somebody set by hand.
