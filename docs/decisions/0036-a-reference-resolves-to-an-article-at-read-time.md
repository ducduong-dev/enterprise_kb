# ADR-0036 — A reference resolves to an article, at read time

**Status:** proposed · **Date:** 2026-08-13 · **Amended:** 2026-08-16, twice, by implementation
(see *Corrections from building it* at the end).

## Context

When a document says *"theo quy định tại Điều 12 Thông tư 41/2016/TT-NHNN"*, the platform parses
it, matches the legal number to the registry, and writes an edge carrying `articles = [12]`. That
column is stored, shown on the inspection screen, and used by the impact traversal to decide
whose owner to interrupt.

Nothing ever turns it into the text of Điều 12.

Graph expansion returns `GraphExpansion(document_id, ref_type, summary, citation_label)` — a
document and a one-line summary. A reader following a citation lands on a forty-article circular
and has to find the article themselves, which is the work the reference was supposed to have
already done. The chatbot has the same problem in a worse form: a reference is an invitation to
quote the cited rule, and what it can reach is a summary of the document containing it.

There is a resolver in the tree, but it answers a different question.
`RetrievalEngine.citation_lookup` takes free text a *user* typed and matches it against
`citation_label` with trigram similarity above a deliberately high threshold — set high because,
as its comment records, loose matching happily paired "Bước 3, QT 07/2024" with "Mục 3, QĐ
114/2023". That fuzziness is right for a human's typing. It is the wrong tool for a reference the
platform parsed itself, where the document is already identified and the article number is
already an integer.

## Decision

A reference resolves to the **clause it actually cites**, in the version the **query's date**
selects, at **read time**, through the **retrieval funnel**.

### The stored anchor is as precise as the citation was

`document_refs.articles` is `INT[]`, and that is coarser than both ends it sits between.
Vietnamese instruments cite clauses and points — *"khoản 2 Điều 12"*, *"điểm a khoản 3 Điều 8"* —
and the chunker's unit is already the **khoản**: `can_merge_with` refuses to merge across an
article boundary, and when it merges two clauses it backs the citation off to their common
ancestor, precisely so that a chunk never cites one location while containing another's text.
So the citation is clause-precise, the chunk is clause-precise, and the only coarse thing in the
chain is the column between them. A reference to one clause of Điều 12 resolves to all four.

The edge therefore carries `anchors TEXT[]` alongside `articles`, in the dotted form
`build_citation_label` already emits — `"12"`, `"12.2"`, `"12.2a"` — so an anchor is directly
comparable to what the target's chunks already carry. `articles` stays as it is: it has a live
consumer in the impact traversal, which asks a genuinely article-shaped question ("does this
policy implement anything that moved?"), and deriving it from the anchors keeps both honest.

Precision then follows the citing text rather than a fixed granularity. An anchor of `"12"`
resolves to every chunk of Điều 12, in `ordinal` order, because that is what the document cited.
`"12.2"` resolves to one.

**The anchor is the article number, not a chunk id.** Chunk ids do not survive: `_insert_chunks`
deletes and re-inserts every chunk of a version on each publish and each rechunk, so a
`dst_chunk_id` on an edge is a dangling pointer one rechunk later. The article number is stated
by the instrument itself and outlives every derived form. This is the same rule ADR-0033 applies
to `clause_supersessions` and `diff.py` applies to alignment, and it is worth stating once as a
platform rule: **derived identifiers are never stored across a boundary that regenerates them.**

**Resolution is an equality join, not a similarity match.** Once ADR-0032 puts `article` on
`chunks`, resolving `(dst_document_id, article)` against the target's canonical, non-tombstoned
chunks is exact. No threshold, no trigram, nothing to tune. `citation_lookup` keeps trigram
matching for the free-text case, which is the only case that needs it.

**It happens in retrieval-api, under the caller's filter (INV-1, INV-2).** A reference to a
restricted document's Điều 12 must resolve for someone who may read it and be invisible to
someone who may not, with existence disclosure decided by the target's category as it is
everywhere else. Note this needs the *chunk* predicate, not the graph one: `compile_sql_graph`
deliberately omits status and effectivity because it answers "may this person know the target
exists". Returning the target's text is a different question, and it takes `compile_sql` with the
full predicate — including effectivity, so a reference into a repealed article resolves to
nothing on the default path and still resolves under `as_of`.

**What comes back is an anchor, not a document.** `GraphExpansion` gains the resolved articles:
chunk id, citation label, and an opening excerpt — enough for the portal to render *"Điều 12.2,
TT 41/2016/TT-NHNN — Tỷ lệ an toàn vốn tối thiểu là 8%…"* as a link, and enough for chat-api to
decide whether the answer needs the full text, which it then fetches through the ordinary path.
Inlining whole articles into every expansion would spend the context budget on references nobody
followed.

**An article that does not resolve is reported, not dropped.** The target may number its articles
differently, the article may have been inserted by an amendment we have not consolidated, or the
reference may simply be wrong. Each is worth a steward's eye and none is worth a silent empty
result — the pending-reference lesson from ADR-0028, applied to the second half of the same
problem.

### Which version the anchor lands in

The query's date decides, exactly as it decides everything else about effectivity.

On the default path that is the target's **current canonical version**: what a reader following
a citation needs is what the rule says now, and quietly resolving into an archived version would
hide the change they most need to see. A consolidation can renumber, so where the edge's
`created_at` predates the target's current `published_at`, the resolved anchor is marked **stale**
— a statement about confidence, not a failure: *this anchor was read from the text when the
target looked different.*

Under `as_of` it is the version canonical **at that date**, and the stale marker does not apply.
Resolving a point-in-time query into today's text would defeat the only thing point-in-time is
for. That path reaches tombstoned chunks, so it inherits the archive treatment already in place —
the `include_tombstoned` filter, the archive-reader role, and the per-hit audit record.

### The same machinery answers the inbound question

`document_refs` read by `dst` with the anchors is *"which clauses elsewhere point at Điều 12 of
this document"*. The inspection screen shows inbound references today at document granularity;
with anchors it shows them against the clause they name. That is what a steward deciding a clause
supersession needs in front of them, and it costs nothing beyond querying the same table the
other way round.

### An anchor is a pointer, not a quotation

The chat surface may name an anchor freely — a reference is public information once the target
passed the filter. Quoting its *text* is a separate act: the text has to be fetched through the
ordinary retrieval path, pass the filter again, and become a real citation. ADR-0018 refuses an
answer with no valid citation, and a summary echoed out of an expansion is not one.

## What this unlocks

Reference resolution is not only a reading convenience; it is the highest-confidence input to
ADR-0033's detection. An edge that names articles gives a **known clause pair** — the citing
clause and the cited article's chunks — with no candidate search and no embeddings involved. That
is Path A in ADR-0033, and it is why the paths are ordered as they are: the corpus already
contains a great deal of clause-level relationship information that has never been resolved to
clauses.

## Consequences

* `chunks.article` is load-bearing for two features now, ADR-0032's flag and this one, which
  strengthens the case for the migration and rechunk landing early in M9b.
* The resolution query runs per expansion, so it needs an index on `chunks(document_id, article)`
  where the version is canonical. That is one composite index, not a projection to maintain.
* An article that spans several chunks resolves to several anchors, ordered by `ordinal` — right,
  because Điều 12 with four clauses *is* four retrievable units and a reader following a
  reference to the article should see them in document order.
* Anchor extraction is a change to reference detection, so it runs over the corpus again. Unlike
  the chunk columns this needs no rechunk: the anchors come from the citing document's stored
  KBDoc, and a re-detection pass writes them onto existing edges. Edges whose anchors cannot be
  recovered keep article granularity, which is today's behaviour.
* `graph_serving` is unchanged. It carries the ACL and summary needed to decide whether an edge
  may be *shown*; the article text is fetched after that decision, not denormalized into it,
  because it would then need refreshing on every publish of every target.

## Corrections from building it

Two claims above did not survive contact with the code. Both are recorded rather than quietly
fixed, because each was a plausible-sounding sentence that would have produced a wrong join.

**The dotted form is not what `build_citation_label` emits.** It emits `"Điều 12.2, TT
41/2016/TT-NHNN"` — the dotted *structure* is there, but the string carries the article word
and the instrument number, and on an English document the word is `Article`. An anchor is a
join key and has to be bare. `build_anchor(path) -> "12.2"` now owns the dotted form and
`build_citation_label` decorates it, so the address a human quotes and the address a reference
resolves through cannot drift apart. One consequence worth stating: a **point with no clause
above it is dropped from the anchor**, because `"12a"` is indistinguishable from Điều 12a — an
inserted article, which Vietnamese amendments create routinely. The label keeps it; a reader
has the surrounding document to disambiguate and a join does not.

**`(dst_document_id, article)` cannot resolve a clause anchor.** Every clause of Điều 12
carries `article = 12`, so that join answers `"12"` exactly and cannot tell `"12.2"` from
`"12.3"` — which this ADR's own worked example requires. `chunks.anchor` (migration 0007)
carries the full dotted address beside `article`, which stays for the article-shaped questions
the supersession flag and the impact traversal ask. Both are pure functions of `section_path`,
so `scripts/backfill_chunk_article.py` fills them without a rechunk or any re-embedding —
the corpus rechunk the milestone budgeted for is not needed for these columns.

**And a resolution rule this exposed.** The chunker merges two short clauses into one chunk and
backs its citation off to their common ancestor, precisely so a chunk never cites one location
while containing another's text. So a reference to *khoản 2 Điều 12* can find no chunk anchored
at `"12.2"` while the text sits under `"12"`. Resolution therefore **falls back from the clause
to its article** before reporting an anchor unresolved. Reporting it would tell a steward that
a perfectly good reference is broken, which is the pending-reference lesson from ADR-0028
pointed at the wrong target.
