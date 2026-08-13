# ADR-0004 — Facet narrowing is set intersection; empty intersection is a violation

**Status:** accepted · **Date:** 2026-08-10

## Context

INV-2 says request facets may narrow but never widen. Two readings were possible for a facet
that names something outside the current scope: silently clamp it, or reject it.

## Decision

Narrowing is set intersection against the resolved filter. If the intersection is non-empty,
the tighter set wins (asking for a parent category while scoped to a child is a no-op, not a
widening). If the intersection is empty — a different department, a category outside every
permitted subtree, an unknown class — the request raises `PolicyViolation`, is audit-logged at
WARN with the attempted values, and increments `kb_policy_violations_total`.

Widening is additionally *unrepresentable*: `RetrieveRequest` and `Facets` forbid extra fields
and have no field for visibility, groups or principal.

## Consequences

Probing is visible rather than silently absorbed: a caller enumerating departments generates
an alertable signal. The cost is that a genuinely mistaken facet returns 403 instead of an
empty result set; that is the correct trade in a bank.
