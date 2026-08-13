# ADR-0025 — A vision model may read every page, if someone signs for it

**Status:** accepted · **Date:** 2026-08-11

## Context

M3 built the scanned route around PaddleOCR with VLM escalation: OCR reads every page, the
confidence scorer flags the pages whose Vietnamese tone marks did not survive, and only those
go to the vision model. That ordering was a cost decision — the VLM is one to two orders of
magnitude more expensive per page — and it still holds for most of the archive.

It does not hold for all of it. The bank's worst material — 1990s photocopies, pages under
heavy stamps, handwritten marginalia, forms whose grid PaddleOCR reconstructs as prose — is
read materially better by a modern vision model reading the page directly, and the public
models (Gemini, GPT-4o, Claude) are currently better at degraded Vietnamese than anything the
bank can host. For a backfill of those documents, escalation is the wrong shape: nearly every
page escalates, so the OCR pass is pure cost and the confidence scorer is deciding nothing.

## Decision

`KB_MODEL_OCR_ENGINE` chooses how a scan is read:

* **`paddle`** (default) — unchanged: OCR every page, escalate what the scorer flags.
* **`vlm`** — the vision model transcribes every page. No OCR pass at all; PaddleOCR need not
  be deployed.

Which vision model that is remains a route (ADR-0024), so the same switch covers "local
Qwen2.5-VL reads everything" and "a public model reads everything". Three consequences are
carried explicitly rather than left implicit:

1. **A public route needs Compliance's ruling.** A page image *is* the document — more so than
   its text, because it carries the letterhead, the signature and the stamp. `route` refuses an
   external model unless `KB_MODEL_ALLOW_EXTERNAL_PROCESSING` is set ([OPEN]-1).
2. **The report says where the pages were read.** `idp_report.warnings` names the model, and
   says plainly when page images left the bank's network. A reviewer looking at a transcription
   a year later can see what produced it, and so can an auditor.
3. **There is no second opinion.** With escalation, a page the scorer distrusts has somewhere
   to go. In vision-first there is nothing above it, so a low-scoring page is a warning on the
   document rather than a silent acceptance — the reviewer is told which pages the system
   itself does not trust.

The scorer runs on vision output exactly as it runs on OCR output. It was built to measure
whether Vietnamese text survived, and that question does not change with the engine.

## Consequences

* A deployment can now run the scanned route with **no OCR engine at all** — the pipeline
  refuses only when neither engine is configured, which is the one case that would otherwise
  produce a document of confidently empty pages.
* Cost moves from "the pages that need it" to "every page", and that is the point of making it
  a setting rather than a default: it is switched on per environment, for a backfill of the
  material that needs it, not left on for the corpus.
* Every block still records its engine (`paddle`, `vlm`, `human`), so a corpus processed both
  ways stays legible — and the review editor already shows the difference.
* Blocks from a vision transcription have no bounding boxes, so the review editor cannot draw a
  box on the page for them. That was already true of escalated pages; vision-first makes it
  true of whole documents, and the reviewer reads the page image alongside instead.
