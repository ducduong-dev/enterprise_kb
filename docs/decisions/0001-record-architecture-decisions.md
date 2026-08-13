# ADR-0001 — Record architecture decisions

**Status:** accepted · **Date:** 2026-08-10

## Context

The implementation plan locks several decisions and defers five (`[OPEN]`). Regulated work
needs the reasoning available years later — for an auditor, for the next team, and for
whoever has to reverse a call.

## Decision

Every architecturally significant choice gets a numbered ADR here: context, decision,
consequences, and what would reverse it. Anything the plan marks `[OPEN]` gets an ADR when it
resolves. Anything the plan leaves unspecified that we decide while building gets one too,
per the plan's own instruction.

## Consequences

A PR making a structural choice without an ADR is incomplete. ADRs are immutable once
accepted; a reversal is a new ADR that supersedes the old one.
