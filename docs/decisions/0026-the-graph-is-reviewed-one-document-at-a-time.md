# ADR-0026 — The graph is reviewed one document at a time

**Status:** accepted · **Date:** 2026-08-11

## Context

The platform builds two things a steward cannot see: the chunks a version was indexed as, and
the edges between documents that the IDP pipeline detected. Both are machine output, both
change what an answer says — chunks are what retrieval returns, and edges are what the
expansion step follows and what the impact traversal walks. Until now the portal showed
neither, so "check the machine's work" meant reading the database.

The obvious way to show a graph is to draw the whole graph: nodes, links, a force simulation.
That was the first thing considered and it is the wrong tool here, for three separate reasons.

At the corpus size this platform is sized for — thousands of instruments, each citing several
others — a whole-graph layout is a hairball. Nothing is legible, and the parts that *are*
legible are legible by accident of where the simulation settled.

It answers no question a reviewer actually has. The questions are always local and always
typed: *what does this circular amend, and has anyone confirmed it? Which internal policies
implement it, so which ones must change when it changes? Is this document still the one in
force?* Those are one-hop or two-hop questions about a named document. A global picture buries
each of them in three thousand irrelevant ones.

And the Artifact/portal CSP blocks external scripts, so a graph library would have to be
vendored into the bundle — carrying a physics engine, and a non-deterministic layout, for a
picture nobody can act on.

## Decision

The graph is reviewed **ego-centrically**, one document at a time, at `/documents/:id`.

**The neighbour view.** The subject document sits in the middle. Incoming edges are on the
left ("what points at this"), outgoing on the right ("what this points at"), grouped by
`ref_type`, because the type *is* the meaning — `amends` and `cites` are not two instances of
a generic link. The rendering is a deterministic inline SVG: no simulation, so the same
document draws the same picture every time and two reviewers describe the same shape. Above
`MAP_NODE_LIMIT` (24) neighbours the map is dropped and the typed lists stand alone; a diagram
of forty nodes is decoration, and the list is the thing that is read anyway.

**The lineage strip.** Amendments and abrogations are shown separately, ordered by effective
date, not by graph position. "What is in force today" is a chronological question, and
answering it from a node-link picture requires the reader to do the sorting in their head.
Each entry carries whether a consolidated version exists, which is the actual decision the
strip drives.

**Every edge carries its provenance.** `detected_by` (the pipeline) and `confirmed_by` (a
person) are shown separately and never merged into one "verified" flag — the whole reason a
steward is looking at this screen is to tell machine output from human judgement. Two actions
follow from that: confirm the edge, or remove it. They are the only writes on the screen.

**Chunks are shown, not edited.** They are derived from the version's KBDoc and rewritten
whole by the publish transaction (INV-5); the version itself is immutable (INV-9). Editing a
chunk in place would make the index disagree with the document it claims to quote, which is
exactly what INV-6 and INV-11 exist to prevent. What is offered instead is **re-chunk**: run
the same version's text through chunking and embedding again, in one transaction, replacing
the chunk set. It is not a publish — the canonical version does not change, no content
changes, so it needs no four-eyes approval and tombstones nothing. It exists for the case the
screen is built to expose: the text was fine and the chunking was not, after a chunker or
embedding-model change. Changing the *text* still means a new version.

## Consequences

* A reviewer can see the corpus only through documents they may read. Edges whose other end is
  outside their ACL are **counted, not named** — hiding them entirely would misrepresent how
  connected a document is, and naming them would make the graph a disclosure channel for
  titles. The count is the honest middle.
* There is no global graph picture, and no way to answer "show me clusters in the corpus" in
  the portal. That is a genuine capability this decision gives up. Impact traversal already
  answers the operational version of it (`what does changing this break`), and a corpus-wide
  view, if it is ever wanted, is an analytics question rather than a review question.
* `rechunk` is a steward-role action that writes to a published version's chunks. It emits the
  same `document.published` outbox event as a publish, so any external index converges rather
  than silently holding the old chunk set.
* The warnings on the screen (published with no chunks, chunks with no vector, unconfirmed
  detected edges, an amendment with no consolidation) are computed from the same data the
  screen shows, so there is no second source to keep in step.
