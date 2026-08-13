# ADR-0029 — An effective date is read, then confirmed

**Status:** accepted · **Date:** 2026-08-12

## Context

`document_versions.effective_from` has existed since the initial schema, and nothing in the
ingest path ever set it. Every document uploaded through the portal was stored with a NULL
effective date; only the seeded fixtures had one, because they are inserted directly.

NULL is not "unknown" to the code that reads it. The compiled ACL predicate is
`effective_from IS NULL OR effective_from <= :date`, so a NULL document is **in force on every
date, forever**. Three features degrade quietly from that:

* **Point-in-time retrieval** (`as_of`, INV-6) returns the whole corpus whatever date is asked
  for, which makes the archive-reader role's one privilege meaningless.
* **The lineage strip** on the inspection screen orders amendments by effective date. With no
  dates it renders *chưa rõ hiệu lực* for every entry, which is what surfaced this.
* **Expiry** — an instrument that ceased to apply has no way to say so.

For a bank, "which version of this rule was in force on the day of the transaction" is not a
nice-to-have; it is the question an auditor asks.

## Decision

The parser reads the instrument's own words, and a human confirms them before publication.

`kb_vntext.dates` extracts two things from the assembled text: the issue date from the
place-and-date line, and the effective date from the effectivity clause — "có hiệu lực thi
hành kể từ ngày 01 tháng 5 năm 2026", numeric or spelled, plus "kể từ ngày ký", which resolves
to the issue date. The result rides on `KBDoc.doc_meta` through the workflow onto the version.

Three properties this is built around:

**It reads, it does not guess.** A document whose effectivity is phrased in a way the module
does not match returns `None`. The nearest date in the text is *not* used as a fallback: an
instrument's final article is full of dates belonging to the documents it repeals, and a
regulation applied from the wrong day is worse than one whose date a reviewer had to type.

**It ignores clauses about other instruments.** "Nghị định số 42/2022/NĐ-CP … hết hiệu lực"
sits in the same article as this document's own effectivity. Matching `hiệu lực` alone would
date a 2026 decree to 2022.

**The reviewer owns the answer.** The review screen shows the detected date *and the sentence
it came from*, so checking it is one glance rather than a document read. The reviewer can
correct it, or clear it. The correction lands on the version being published — via
`RegistryService.set_effective_from`, which refuses a version that is already published,
because the chunks carry a copy of these dates and moving them under a published version would
change what a past query would have returned (INV-9).

## Consequences

* Documents ingested before this change still have NULL dates. They are corrected by
  publishing a new version, or accepted as "always in force" — which for an internal procedure
  is often the truth. There is deliberately no bulk backfill: a date nobody read is exactly
  what this ADR exists to avoid.
* An empty effective date remains legal and still means "in force from the beginning of
  records". The difference is that it is now a reviewer's decision rather than an omission,
  and the review screen says so in words.
* The detector is Vietnamese-only. A bilingual instrument states effectivity in the Vietnamese
  text, which is the authoritative one, so this is a limitation rather than a gap.
* `effective_to` is still unset by ingest. It is not stated in a document's own text — it
  arrives when a later instrument abrogates this one — so it belongs to the consolidation
  flow, not here.
