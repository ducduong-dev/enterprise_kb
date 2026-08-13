# ADR-0005 — Store the original before starting the workflow; never put bytes in a workflow

**Status:** accepted · **Date:** 2026-08-10

## Context

The plan describes `IngestWorkflow` as "store original → IDP activity → …". That leaves open
whether the document bytes arrive as a workflow argument or are stored first and referenced.

Temporal persists every workflow argument and activity result in the workflow history. A
50 MB scanned circular passed as an argument would be written to Temporal's datastore, replayed
on every worker restart, and retained for the life of the workflow — which, with human review
steps, is weeks. Temporal also caps payload size well below the largest documents in the corpus.

## Decision

`portal-api` writes the upload to object storage first (content-hash addressed), then starts
`IngestWorkflow` with an `UploadRef` — bucket, key, hash, filename, size. Activities fetch the
bytes from storage when they need them. The same rule applies to derived artifacts: the KBDoc
is written to the derived bucket and only its reference travels through the workflow.

## Consequences

A workflow that never starts leaves an object with no version pointing at it. That is harmless:
the key is the content hash, so re-uploading the same file reuses the object rather than
orphaning another. A periodic sweep can reclaim unreferenced objects; nothing depends on it.

The upload endpoint stays useful during a Temporal outage — the bytes are already durable, and
only the trigger is lost.
