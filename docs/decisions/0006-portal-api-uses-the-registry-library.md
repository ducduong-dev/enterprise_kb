# ADR-0006 — portal-api and the workflow activities use the registry *library*, not its HTTP API

**Status:** accepted · **Date:** 2026-08-10

## Context

`kb-registry` is a service with an HTTP surface. `portal-api` and the Temporal activities also
need to create documents and versions, list review tasks, and (from M2) run the publish
transaction. Calling registry over HTTP would add a network hop and, more importantly, put a
transaction boundary in the wrong place: the publish path (INV-5) must commit registry writes,
chunk tombstoning and the outbox event in *one* Postgres transaction.

## Decision

`kb-registry` ships as a workspace library (`RegistryService`, `repository`) that other
services import, plus a thin HTTP service that exposes the same operations for tools and
future consumers. In-process callers take a `Session` and control the transaction; the HTTP
service is a caller like any other.

## Consequences

Registry rules — classification defaults, retention, duplicate detection — have one
implementation regardless of entry point. The trade-off is that the database is shared by
several services rather than owned by one; the plan already assumes a single Postgres, and the
invariant lint keeps the *read* paths funnelled through retrieval-api (INV-1) where it matters.

If a service ever needs to run separately from the registry schema, it uses the HTTP surface;
that is why it exists rather than being deleted.
