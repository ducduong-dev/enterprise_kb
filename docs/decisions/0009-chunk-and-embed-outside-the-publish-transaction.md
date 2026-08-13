# ADR-0009 — Chunk and embed before the transaction; write inside it

**Status:** accepted · **Date:** 2026-08-11

## Context

Two statements in the plan pull in different directions. INV-5 requires the canonical flip,
old-chunk tombstoning, **new-chunk insert**, edge updates and the outbox event to commit in one
Postgres transaction. Section 7 describes the indexer chunking and embedding *after* consuming
the publish event.

Taken literally together they would put an embedding call — a network round trip to a GPU node,
hundreds of milliseconds to seconds for a long document — inside a transaction that holds a
lock on the document being published.

## Decision

Split the work at the transaction boundary:

1. `PublishService.prepare()` chunks and embeds. No transaction is open. Slow, retryable, and
   free of locks.
2. `PublishService.publish()` opens one transaction, locks the document row, re-checks the
   guards, and writes everything INV-5 lists — including the prepared chunk rows with their
   vectors — then commits.
3. The indexer consumes the outbox event and mirrors the canonical chunks into the **external**
   keyword index, which cannot join a Postgres transaction.

Postgres (registry + pgvector) is therefore consistent the instant the publish commits. Only
the external index converges asynchronously, and `kb_publish_to_searchable_seconds` measures
exactly that gap against the 10 s budget.

## Consequences

A publish whose preparation succeeded but whose transaction failed leaves nothing behind —
the chunks were never written. A publish that commits is immediately correct for every read
path served from Postgres, which in the default configuration is all of them.

The row lock (rather than SERIALIZABLE isolation) is what serializes concurrent publishes of
one document: isolation level must be set before a transaction's first statement, which would
forbid composing publish with a caller that has already read, and it aborts on unrelated
conflicts. The partial unique index on `is_canonical` remains the backstop.
