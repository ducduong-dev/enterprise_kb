# ADR-0002 — SQLAlchemy models live in `libs/schemas`

**Status:** accepted · **Date:** 2026-08-10

## Context

Four services write to the registry database (registry, indexer, portal-api, workflows) and
two more read from it. The plan's layout gives `libs/schemas` the shared Pydantic models but
does not say where the ORM mapping lives. Putting it in `services/registry` would make every
other service import a service package; duplicating it would let the copies drift.

## Decision

`kb_schemas.orm` holds the SQLAlchemy mapping and owns `metadata`; Alembic's `env.py` imports
it. Pydantic models remain the exchange format between services — ORM rows never cross a
service boundary.

## Consequences

`libs/schemas` depends on SQLAlchemy, so any process importing it pulls that in; acceptable,
since every service already talks to Postgres. `make check-drift` runs `alembic check` against
a live database so the mapping and the migrations cannot silently diverge.
