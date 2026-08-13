# ADR-0017 — Approvals are recorded in the portal and signalled to the workflow

**Status:** accepted · **Date:** 2026-08-11

## Context

`MergeFlow` waits for approval signals and publishes when four-eyes is satisfied. The portal
is where a person clicks. That leaves a question with three plausible answers: who owns the
approval ledger?

* **Only the workflow.** The portal forwards a signal and shows whatever the workflow query
  returns. A self-approval or a duplicate click is then refused *inside* the workflow, where
  the refusal is a log line — the approver sees their click accepted and nothing happen.
* **Only the portal.** The portal counts approvals and calls publish itself. That creates a
  second publish path, which is exactly what INV-5 exists to prevent.
* **Both, with one authority.**

## Decision

Both, with the workflow as the authority over publishing and the portal as the authority over
the reply the approver reads.

`POST /v1/merge-tasks/{id}/decision` runs the approval rules (`ApprovalLedger`) against the
approvals stored on the review task, refuses with `409` and a readable reason — "the preparer
may not approve their own merge", "this person has already approved", "the draft changed after
this approval was given" — and only then signals the workflow. The workflow runs the identical
rules on arrival, and it alone calls `publish_merged`.

The ledger is persisted on the review task's `payload`, not held in memory, so a reviewer who
reloads the page sees what has already been approved.

## Consequences

* The decision is durable before it is delivered. If Temporal is unreachable, the approval is
  stored and the response says `signalled: false`; the workflow can be signalled again, and no
  approval is lost because a message broker was down.
* The rules exist in one module (`kb_workflows.merge_policy`) and are imported by both callers,
  so "the portal and the workflow disagree about four-eyes" is not a state the system can
  reach.
* Replaying stored approvals into a fresh ledger deliberately skips the guards: they were
  checked when each approval arrived, and re-checking them on replay would reject a legitimate
  second approver.
