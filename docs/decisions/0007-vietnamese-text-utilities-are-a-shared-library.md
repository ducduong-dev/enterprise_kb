# ADR-0007 — Vietnamese text handling lives in `libs/vntext`

**Status:** accepted · **Date:** 2026-08-10

## Context

Legal-number extraction, document-structure detection (Chương/Điều/Khoản) and language
detection are needed by IDP at parse time (M1) and by identity resolution and consolidation at
merge time (M5). The plan lists the legal-number extractor under M5, but the IDP cannot emit
`KBDoc.doc_meta.legal_number` or `detected_refs` without it.

Two copies would drift, and the failure mode of drift here is silent: M1 would record a
document under one normalization and M5 would look it up under another, so a revision of an
instrument would not be recognized as the same instrument.

## Decision

`libs/vntext` owns legal numbers, section structure and language detection. It has no
dependencies beyond the standard library, so it stays cheap to import anywhere.

Its normalization rule is narrow on purpose: diacritics are folded **only** in instrument type
codes (QĐ ≡ QD), because both spellings appear in filenames, OCR output and search boxes.
Document text is never normalized — the byte-level fixture test exists to prove it.

## Consequences

M5's identity resolution extends this library rather than starting its own extractor; the
matching layers and thresholds it adds sit on top of the same normalized key. M1 deliberately
implements only exact-match identity: fuzzy matching is a judgment call that needs a review
task behind it.
