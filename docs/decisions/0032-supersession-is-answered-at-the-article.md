# ADR-0032 — Supersession is answered at the article, not the document

**Status:** proposed · **Date:** 2026-08-13

## Context

Since M2 an unconsolidated `amends` or `abrogates` edge flags its target, and ADR-0015 settled
how the flag clears. What it did not settle is *how much* of the target is flagged: the
predicate returns document ids, and every chunk of the document comes back with
`supersession_flag = True`.

An amendment usually touches two or three articles of a sixty-article circular. The reader gets
a warning on all sixty, including the fifty-seven the amendment never mentions. The predictable
result is that the warning stops being read — and the one article it was right about is the one
nobody notices.

The data to do better is already in the database and already trusted by another consumer.
`document_refs.articles` records which articles a reference touches; the change-impact traversal
filters on it (`impact.assess`: *"a policy implementing Điều 12 is not affected by a change to
Điều 40, and telling its owner otherwise is how impact tasks get ignored"*). That reasoning is
the same reasoning, one hop earlier, and the supersession predicate ignores the column.

## Decision

The predicate returns articles, not just documents, and a chunk is flagged when its own article
is among them.

* `RetrievalEngine._superseded_documents` becomes `_superseded_articles`, returning
  `document_id → set[int]`. An **empty set means the whole document**, which is exactly today's
  behaviour, so an edge with no article detail loses nothing.
* Chunks carry their article number in a denormalized `chunks.article INT` column, written by
  the chunker. `kb_vntext.sections` already parses `Điều N` into the section path and
  `diff._article_number` already recovers the integer from a path; neither the SQL layer nor the
  retrieval hot path should be re-deriving it with a regex over a text column.
* `kb_registry.publish.SUPERSEDED_PREDICATE` and its bulk twin move together. ADR-0015 accepted
  that the predicate exists twice on purpose; the drift test in `tests/test_consolidation_flow.py`
  now has to assert the two agree at article granularity, not just on the document set.
* `is_superseded(session, document_id)` keeps its present meaning — *any* unconsolidated
  amendment targets this document. That is the workflow question ("does this need a
  consolidation?"), and it is document-shaped. Only the retrieval-facing answer narrows.

Populating the column is a migration plus a corpus rechunk. `PublishService.rechunk` exists for
precisely this case (ADR-0026): the derived form changed, the text did not, so no four-eyes and
no PII re-run.

## Consequences

* The warning becomes quotable: "Điều 12 of this circular is amended by X and the consolidation
  is not approved" instead of a banner over the whole instrument. `chat-api`'s generic
  `SUPERSESSION_WARNING` can name the article.
* **The failure direction flips, and this is the risk.** Today the flag over-warns; afterwards a
  wrong or missing article list under-warns, which is worse. Two things hold it: an edge with no
  articles still flags everything, and article detection is part of reference confirmation on the
  review screen, where a human already looks at the edge.
* A chunk that spans no article — front matter, an appendix, a table before Điều 1 — has a NULL
  article and is flagged only when the whole document is. That is the right default: an
  amendment to Điều 12 does not put the appendix out of date.
* This is the cheapest change in the expiry programme: no models, no new tables, data already
  collected. It should land before ADR-0033's detection work, because it establishes the
  granularity that work produces its output at.
