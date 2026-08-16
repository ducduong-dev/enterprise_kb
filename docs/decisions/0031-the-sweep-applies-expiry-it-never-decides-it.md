# ADR-0031 — The sweep applies expiry, it never decides it

**Status:** proposed · **Date:** 2026-08-13

## Context

Expiry is the one fact in this domain that becomes true while everybody is asleep. Every other
state change in the platform is caused by somebody doing something — an upload, an approval, a
publish — and has a transaction to hang correctness on. A document ceasing to apply on 1 May
has no such event.

The platform has nothing that notices. There is no cron, no Temporal Schedule, no sweep
anywhere in the tree. Three things need one:

* `documents.status` never becomes `expired`. The value is in the enum and in the Postgres
  type, and `FilterBuilder` admits it for archive-readers under `as_of` — a privilege over a
  set that cannot be populated.
* Nobody is warned *before* an expiry lands. A regulation whose sunset date is known six months
  out should put a task in front of its steward, not surprise them.
* `documents.review_by` is settable, indexed, and read by nothing. Periodic re-attestation of
  internal procedures is the reason the column exists.

A scheduled job that the corpus depends on is also a liability. If the answer to "why was a
withdrawn fee served to a customer yesterday" is "the nightly job did not run", the design is
wrong, not the operations.

## Decision

**Serving correctness never depends on the job running.**

The ledger's dates are projected onto `chunks` and `document_versions`' serving copies at the
moment a ledger row is *confirmed* — inside that transaction, not on the day the date arrives.
The ACL predicate already compares `effective_to` against the query date, so a chunk whose end
date was written in February stops being retrievable on 1 May with nothing running at all. A
sweep that is down for a week serves nothing it should not.

What the daily `expiry_sweep` Temporal Schedule does is the bookkeeping only a calendar can
trigger:

* flips `documents.status` to `expired` where a confirmed ledger date has passed;
* opens a review task at **T-30 days** for confirmed expiries approaching, and for documents
  whose `review_by` has come round;
* emits the existing `registry.published` outbox event for the affected documents, so a keyword
  backend living outside Postgres follows.

And what it never does is decide. Every judgement — is this instrument really abrogated, on
what date, on whose authority — happened when the ledger row was written, in front of a person
(ADR-0030). The sweep reads confirmed rows and a clock. That is what makes it safe to run
unattended against a bank's corpus, and it makes the job idempotent: running it twice, or
running it after a three-day outage, produces the same state.

A Temporal Schedule rather than a new service or a cron container: the worker and the schedule
machinery are already deployed, and a workflow gives the run a history somebody can read.

## Consequences

* The failure mode of the sweep is *lateness of banners and queues*, never *wrongness of
  answers*. That is the property to test for: a test that freezes the clock past an expiry,
  never runs the sweep, and asserts the chunk is already unretrievable.
* `documents.status = 'expired'` becomes reachable, and the archive-reader role acquires the
  privilege it was written for.
* Status and the ledger are briefly out of step by design — a document expires at midnight and
  is relabelled when the sweep runs. Nothing reads status for effectivity, so this is
  cosmetic; the invariant is that nothing ever *starts* reading status for effectivity.
* This introduces the platform's first scheduled job. The property above — serving correctness
  is independent of scheduled work — is worth promoting to a numbered invariant if a second one
  ever appears.
