# ADR-0012 — A reviewer's corrections create a new version

**Status:** accepted · **Date:** 2026-08-11

## Context

A scanned document arrives with OCR errors. The reviewer fixes them. The obvious
implementation — edit the version's text in place — is forbidden by INV-9 (versions are
immutable) and, less obviously, would destroy the only record of what the scanner actually
produced.

## Decision

Corrections produce a **new version**, `source_type='portal_edit'`, authored by the reviewer,
whose `idp_report_ref` points at the corrected KBDoc. The OCR output stays exactly as it was,
canonical-flag cleared, as the record of what the machine read.

Two consequences follow, both intended:

* **Four-eyes becomes real on regulated documents.** The reviewer who corrected the text is
  its author, so they cannot approve it (INV-8) — a second reviewer must. For operational
  documents a single reviewer corrects and publishes in one step, which is the M3 acceptance
  criterion; for a circular, the four-eyes rule applies and the API says so.
* **A corrected version inherits its predecessor's PII state.** Otherwise correcting a blocked
  document would launder it: `blocked` → new version → `pending` → cleared, with no gate ever
  run on the corrected text.

## Consequences

A heavily corrected scan produces two versions, and an auditor can see precisely what the
machine read and what the human changed. The cost is version churn on documents that need many
passes; if that becomes a problem the answer is batching a review session into one submission,
not making versions mutable.
