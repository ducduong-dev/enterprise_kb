#!/usr/bin/env python
"""Golden-set runner — retrieval quality and the ACL sweep in one pass.

They are the same run on purpose. Every query is issued as its fixture principal through
`retrieval-api`'s engine, then scored two ways:

* **Quality**: recall@k and nDCG@k against the graded set. Regressions block merges to main.
* **Access**: any document on the query's `forbidden` list that comes back is a violation.
  Violations are not a score — one fails the run outright, whatever the quality numbers say.

Run against the seeded corpus:

    make seed && make eval

The dev/CI embedding adapter is a lexical projection, not BGE-M3 (see
`kb_ports.adapters.embedding_hashed`), so the absolute numbers here are a floor, not a
forecast. What they do prove is that the pipeline retrieves the right documents for the right
principals — which is what the M2 acceptance criteria are about.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT))

from metrics import (  # noqa: E402
    acl_violations,
    fact_coverage,
    ndcg_at_k,
    recall_at_k,
    summarize,
    summarize_coverage,
)

#: M2 acceptance criterion.
RECALL_THRESHOLD = 0.85
#: M10 acceptance criterion (ADR-0037). Gated only over the entries that carry a `facts` label;
#: with a handful of them one miss moves this a long way, so the run prints `facts_measured`
#: beside it and a reader should treat the two as one number.
COVERAGE_THRESHOLD = 0.95
DEFAULT_K = 10


@dataclass
class QueryResult:
    query_id: str
    principal: str
    retrieved: list[str]
    grades: dict[str, int]
    forbidden: list[str]
    violations: list[str]
    recall: float
    ndcg: float
    #: Every document the response put in front of the reader — ranked chunks *and* fact-set
    #: members. Distinct from `retrieved`, which is the ranking alone, because the ACL sweep
    #: has to see both: a fact set is a second query and a leak through it is still a leak.
    reached: list[str] = field(default_factory=list)
    #: None when the entry carries no fact label. Not 0.0 and not 1.0 — see `fact_coverage`.
    coverage: float | None = None


def load_golden(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for file in sorted(path.glob("*.yaml")):
        entries.extend(yaml.safe_load(file.read_text(encoding="utf-8")) or [])
    return entries


def validate(entries: list[dict[str, Any]]) -> list[str]:
    from kb_authz.fixtures import ALL_PRINCIPALS

    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        qid = entry.get("id", "<missing id>")
        if qid in seen:
            problems.append(f"{qid}: duplicate id")
        seen.add(qid)
        if entry.get("principal") not in ALL_PRINCIPALS:
            problems.append(f"{qid}: unknown principal {entry.get('principal')!r}")
        if not entry.get("query"):
            problems.append(f"{qid}: empty query")
        for item in entry.get("relevant") or []:
            if item.get("grade") not in (0, 1, 2, 3):
                problems.append(f"{qid}: grade must be 0-3, got {item.get('grade')!r}")
    return problems


def document_key_map() -> dict[str, str]:
    """Seeded document id → golden-set key. The golden set names documents by key so it
    survives a reseed; ids are regenerated deterministically but the key is what a human
    grading a query actually wrote down."""
    from scripts.seed import CANARIES, DOCS, sid

    return {str(sid(f"doc:{doc.key}")): doc.key for doc in (*DOCS, *CANARIES)}


def build_engine(session: Any) -> Any:
    from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
    from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
    from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
    from kb_ports.adapters.rerank import LexicalRerankAdapter
    from kb_retrieval_api.engine import RetrievalEngine

    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
    )


def run_entry(engine: Any, entry: dict[str, Any], keys: dict[str, str], k: int) -> QueryResult:
    from kb_authz.fixtures import ALL_PRINCIPALS
    from kb_schemas.api import RetrieveRequest

    principal = ALL_PRINCIPALS[entry["principal"]]
    response = engine.retrieve(
        principal,
        # Coverage on, always: the ACL sweep runs over this same call, and a channel that is
        # never exercised is a channel never swept.
        RetrieveRequest(query=entry["query"], top_k=k, cover_facts=True),
    ).response

    # Document-level grading: the seed corpus has one graded passage per document, and a
    # citation the user can follow is the unit that matters. Passage-level grading arrives
    # with the real corpus in M7.
    retrieved: list[str] = []
    for chunk in response.chunks:
        key = keys.get(str(chunk.document_id), str(chunk.document_id))
        if key not in retrieved:
            retrieved.append(key)

    # The union: ranking plus coverage. Ranking keeps its own list because recall and nDCG
    # measure the ranking and must not be flattered by a second query.
    reached = list(retrieved)
    for fact_set in response.fact_sets:
        for member in fact_set.members:
            key = keys.get(str(member.document_id), str(member.document_id))
            if key not in reached:
                reached.append(key)

    grades = {item["doc"]: int(item["grade"]) for item in entry.get("relevant") or []}
    forbidden = list(entry.get("forbidden") or [])
    relevant = [doc for doc, grade in grades.items() if grade > 0]

    return QueryResult(
        query_id=entry["id"],
        principal=entry["principal"],
        retrieved=retrieved,
        grades=grades,
        forbidden=forbidden,
        # Swept over the union: a document the filter should have withheld is a violation
        # whether ranking surfaced it or a fact set did.
        violations=acl_violations(reached, forbidden),
        recall=recall_at_k(retrieved, relevant, k),
        ndcg=ndcg_at_k(retrieved, grades, k),
        reached=reached,
        coverage=fact_coverage(reached, entry.get("facts") or []),
    )


def evaluate(entries: list[dict[str, Any]], k: int = DEFAULT_K) -> list[QueryResult]:
    from kb_common.config import get_settings
    from kb_common.db import create_db_engine
    from sqlalchemy.orm import Session

    keys = document_key_map()
    db = create_db_engine(get_settings().db)
    try:
        with Session(db) as session:
            engine = build_engine(session)
            return [run_entry(engine, entry, keys, k) for entry in entries]
    finally:
        db.dispose()


def report(results: list[QueryResult], k: int) -> dict[str, float]:
    summary = summarize(
        [(result.retrieved, result.grades, result.forbidden) for result in results], k=k
    )
    summary.update(summarize_coverage([result.coverage for result in results]))
    print(f"\n{'query':<8} {'principal':<24} {'recall':>7} {'ndcg':>7} {'cover':>7}  retrieved")
    for result in results:
        flag = "  ACL VIOLATION" if result.violations else ""
        # "—" and "0.00" mean opposite things: no fact label, versus a labelled fact this
        # response missed entirely.
        cover = f"{result.coverage:>7.2f}" if result.coverage is not None else f"{'—':>7}"
        print(
            f"{result.query_id:<8} {result.principal:<24} "
            f"{result.recall:>7.2f} {result.ndcg:>7.2f} {cover}  "
            f"{', '.join(result.retrieved[:4]) or '—'}{flag}"
        )
    print("\n" + "  ".join(f"{name}={value:.3f}" for name, value in summary.items()))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=ROOT / "eval" / "golden_set")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--dry-run", action="store_true", help="validate the set only")
    parser.add_argument("--threshold", type=float, default=RECALL_THRESHOLD)
    parser.add_argument("--coverage-threshold", type=float, default=COVERAGE_THRESHOLD)
    args = parser.parse_args()

    entries = load_golden(args.golden)
    problems = validate(entries)
    if problems:
        print("golden set is invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"golden set ok — {len(entries)} queries")

    if args.dry_run:
        return 0

    results = evaluate(entries, args.k)
    summary = report(results, args.k)

    violations = [result for result in results if result.violations]
    if violations:
        print(
            f"\nFAILED: {len(violations)} ACL violation(s) — "
            f"{', '.join(v.query_id for v in violations)}",
            file=sys.stderr,
        )
        return 2

    recall = summary[f"recall@{args.k}"]
    if recall < args.threshold:
        print(
            f"\nFAILED: recall@{args.k} {recall:.3f} below the {args.threshold} threshold",
            file=sys.stderr,
        )
        return 3

    measured = summary["facts_measured"]
    coverage = summary["fact_coverage"]
    if measured and coverage < args.coverage_threshold:
        print(
            f"\nFAILED: fact coverage {coverage:.3f} over {measured:.0f} labelled "
            f"fact(s), below the {args.coverage_threshold} threshold",
            file=sys.stderr,
        )
        return 4
    if not measured:
        # Not a pass. The same distinction eval/harness/supersession.py draws: a metric with an
        # empty denominator is a statement about the fixtures, and printing it as green would
        # let the label set be emptied without anything going red.
        print("\nNOTE: no entry carries a `facts` label — fact coverage was not measured")

    print(
        f"\nOK: recall@{args.k}={recall:.3f}, no ACL violations"
        + (
            f", fact coverage={coverage:.3f} over {measured:.0f} labelled fact(s)"
            if measured
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
