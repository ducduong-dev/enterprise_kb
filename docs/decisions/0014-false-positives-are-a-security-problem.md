# ADR-0014 — Forty clean documents are as much of the criterion as forty dirty ones

**Status:** accepted · **Date:** 2026-08-11

## Context

The M4 acceptance criterion names both halves: 40 seeded-PII documents blocked, 40 clean
documents passed. It would be easy to treat the second half as a nicety. It is not.

A gate that blocks regulatory text — because a circular number looks like an account number,
because a fee table is full of amounts, because a procedure names the officer who approves
things — produces a queue of blocks that are all wrong. Reviewers learn that a block means
nothing and start overriding on reflex. The one document that mattered goes through with the
rest, and the audit trail records a human decision that was never really made.

## Decision

Every rule states what it requires *around* a match, and the clean half of the corpus is
composed of exactly the cases that would otherwise fire:

* instrument numbers (`41/2016/TT-NHNN`, `QD-2023-114`) near words like "số";
* aggregate statistics with long digit runs — excluded by statistical context words;
* fee tables and interest rates — amounts without a person attached;
* role mailboxes (`risk@`, `cskh@`) — excluded by local-part;
* bank staff named while performing a duty — a name is not PII, a name beside a balance is;
* blank form templates, which contain every PII label and no PII.

Two bugs this discipline found, both of which would have shipped otherwise: `"khoản"` (clause)
is a substring of `"tài khoản"` (account), so a substring-matching exclusion list silently
disqualified **every** account number by way of its own label; and requiring the contiguous
phrase `"số tài khoản"` missed `"Tài khoản nghi vấn số ..."`, which is how a fraud note
actually reads.

## Consequences

Rules are narrower and require more context, which means a determined leak — an account number
written with no label and no surrounding words — can pass the patterns. That case is what the
LLM detector and the reviewer are for. The corpus is the regression test for both halves, and
Compliance owns it: a new false positive in production is a new entry in `clean_documents.yaml`
before it is a code change.
