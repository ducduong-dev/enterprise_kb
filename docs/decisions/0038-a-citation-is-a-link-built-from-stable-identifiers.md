# ADR-0038 — A citation is a link, built from stable identifiers

**Status:** proposed · **Date:** 2026-08-16

## Context

`Citation` carries `marker`, `label`, `document_id`, `version_id`, `chunk_id`, `section_path` and
a trimmed `quote`. Everything needed to *check* a claim is in there. Nothing in there is a place
a reader can open.

So the reader gets *"Điều 12.2, TT 41/2016/TT-NHNN"* and a quote, and to see the clause in its
document they search the portal for the label — the work the citation was supposed to have
already done. This is ADR-0036's complaint about graph expansion, arriving at the other end of
the same pipeline: the platform knows exactly which clause of which version it drew the sentence
from, and hands the reader a string.

The payload does contain one identifier that would address the passage directly, and it is the
one that must not be used. `_insert_chunks` deletes and re-inserts every chunk of a version on
each publish and each rechunk, so a link on `chunk_id` breaks at the next chunker improvement —
and breaks for every saved answer at once, silently, because a dead chunk id is
indistinguishable from a chunk the caller may not see.

## Decision

Every citation carries a `link`, and the link is built from `(document_id, version_id,
section_path)`.

**Never the chunk id.** `chunk_id` stays in the payload: it is what joins the citation to the
audit record (INV-11) and it is a retrieval fact, not an address. This is the platform rule
ADR-0036 named — *derived identifiers are never stored across a boundary that regenerates them* —
and a link inside a saved answer is exactly such a boundary crossing.

**The link names the version the answer was drawn from.** A link in a six-month-old answer opens
what was actually cited, not what the document says today; the document view marks it when that
version is no longer canonical and offers the current one. An answer log that promises
reconstructability (INV-11) and links that quietly re-point are not compatible.

**The link is checked, not trusted.** It resolves to the portal document route, which calls
`GET /v1/documents/{id}` — the render-time access re-check that already exists as gate 3, and
already logs archived-version access. A link is not a capability: forwarded to a colleague, it is
evaluated against *their* principal and may show them nothing.

**Two lists, and they answer different questions.** The `[n]` markers inline in the answer are
what the model claims, and `verify` checks each against its passage; an answer with no valid
marker is still a refusal (ADR-0018). Alongside them the response carries `sources`: every
document that contributed a passage to the context, each with a link, whether or not the model
marked it. The first list is about the claims. The second is about what was read — which is the
half a reader needs to judge whether the answer covered the ground (ADR-0037), and it is not the
model's to curate.

**On the external surface a link points only at the public document view.** The fact set was
already scoped to `visibility='external' AND status='published'` server-side (INV-4), so there is
nothing to leak; what remains is a rendering rule — where a category's existence disclosure is
off, an unlinkable source is omitted from the list entirely rather than shown as a bare label,
because a label with no link is still a disclosure that the document exists (ADR-0023).

## Why not deep-link by chunk id and repair the breakage

A redirect table from old chunk id to new would work and is a second identity system to own,
populated on every rechunk, for identifiers whose whole purpose is to be disposable. The section
path is what the corpus itself uses to address a clause, `diff.py` and `clause_supersessions` and
`document_refs.anchors` all already anchor on it, and a fourth thing anchoring on the same stable
key is cheaper than a fifth thing anchoring on an unstable one.

## Consequences

* `Citation` gains `link` and `document_title`; `ChatResponse` gains `sources`. Both are additive.
* Saved answers stay resolvable across rechunks and re-embeddings, which is the property that
  makes an answer log worth keeping.
* A section path that a later version renames leaves a link that resolves to the document but not
  the clause. The view says so — *this version does not contain that section* — rather than
  scrolling to the top and pretending. Same lesson as the unresolved anchor in ADR-0036.
* The portal needs a route that accepts a section path and scrolls to it, which means the
  document view must render from chunks in `ordinal` order rather than the stored original. That
  is the larger piece of frontend work in this ADR.
* A test asserts the link survives `PublishService.rechunk`: build an answer, rechunk the cited
  version, resolve the link again. It is the only failure mode here that a reviewer will not
  notice by looking.
