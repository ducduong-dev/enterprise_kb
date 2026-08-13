# ADR-0003 — Denormalize category and class onto chunks

**Status:** accepted · **Date:** 2026-08-10

## Context

INV-2 requires the ACL filter to be applied *inside* the index query. The plan's `chunks`
table already denormalizes `visibility`, `allowed_groups`, `department`, `doc_status` and the
effective dates for exactly that reason. But `/v1/retrieve` also accepts `category` and
`doc_class` facets, and those columns live only on `documents`.

In Postgres that would be a join; in OpenSearch, whose unit is the chunk, there is no join at
all. Applying those two facets after the query would put a filter outside the index —
precisely what INV-2 forbids, and result counts would leak existence even when text does not.

## Decision

`chunks` also carries `category_path` (ltree) and `doc_class`, written by the indexer from the
document at chunk time. The OpenSearch mapping additionally indexes `category_ancestors` (every
ltree prefix), so subtree containment is a `terms` lookup rather than a prefix query — a prefix
query would match `regulations.sbvx` for a `regulations.sbv` scope.

## Consequences

Reclassifying a document (moving its category, changing its class or ACL) must rewrite its
chunks. That path runs through the publish transaction, which already rewrites chunks, so the
cost is bounded — but a reclassification that skips the indexer would leave stale ACL data in
the index, which is why chunk writes go only through the outbox consumer.
