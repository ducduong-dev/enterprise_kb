# ADR-0008 — Chunk boundaries are structural, not a token window

**Status:** accepted · **Date:** 2026-08-11

## Context

The usual default is a fixed token window with overlap. For this corpus it is the wrong unit.
A Vietnamese instrument is written, amended and cited at the clause level: "Điều 12 Khoản 2".
A window that straddles two clauses produces an answer the reader cannot verify, because the
citation attached to it names a location that does not contain all of the text.

## Decision

One clause (Khoản), or one article (Điều) where it has no clauses. Around that:

* short neighbouring clauses **merge**, and the citation backs off to their common ancestor —
  a chunk containing Khoản 1 and Khoản 2 is cited at the article, never at Khoản 1;
* an over-long clause **splits**, and every part keeps the same citation, because a split is
  an embedding concern and never a citation concern;
* tables are never split or merged into prose, except that a heading immediately above a table
  is kept as its caption;
* a bare heading is never a chunk on its own — it matches every query about its topic and
  answers none;
* every chunk carries its ancestors as a prefix line, so a clause reading "tỷ lệ này" still
  matches a query naming the article.

## Consequences

Chunk sizes vary widely, which is fine for BGE-M3 and slightly awkward for the reranker's
batching. The citation label is trustworthy by construction: `build_citation_label` derives it
from the same section path the chunk was cut on, so a chunk cannot cite a location whose text
it does not contain. That property is asserted directly in the chunker tests.
