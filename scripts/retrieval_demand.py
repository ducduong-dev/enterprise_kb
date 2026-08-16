"""What the corpus is actually being asked for, and how much of that is stale.

Run this *before* deciding how much the clause-supersession funnel is worth. `audit_log` has
recorded every chunk returned by every query since M2 (INV-11), so the question ADR-0033 says
to answer first — "which stale clauses are we serving, and how often" — needs no new machinery.

Two numbers decide the answer:

* **records** — retrievals in the window. Zero means nobody has used the platform yet, which
  looks identical in every other output to "nothing stale is being served" and means the
  opposite. Do not read the rest of the report until this is non-trivial.
* **resolvable share** — how much of the retrieval history still points at chunks that exist.
  Chunk ids do not survive a rechunk, so a low share means the counts understate demand rather
  than that demand is low.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from kb_common.db import create_db_engine
from kb_registry.demand import DEFAULT_WINDOW, clause_demand
from sqlalchemy.orm import Session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW.days)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    since = datetime.now(UTC) - timedelta(days=args.days)
    with Session(create_db_engine()) as session:
        report = clause_demand(session, since=since, limit=args.limit)

    print(f"window            {args.days} days, since {report.since:%Y-%m-%d}")
    print(f"retrieve records  {report.records}")
    print(f"resolvable share  {report.resolvable_share:.0%}", end="")
    print(f"  ({report.unresolved_retrievals} retrievals point at chunks that no longer exist)")
    print()

    if not report.records:
        print("No retrievals logged in this window.")
        print("That is not evidence that nothing stale is served — it is evidence that nothing")
        print("has been asked. Size the funnel from real traffic, not from this.")
        return 0

    if not report.clauses:
        print("Retrievals logged, but none resolve to a clause that still exists.")
        return 0

    width = max(len(item.document_title) for item in report.clauses)
    print(f"{'served':>7}  {'document':<{min(width, 50)}}  clause")
    for item in report.clauses:
        print(
            f"{item.retrievals:>7}  {item.document_title[:50]:<{min(width, 50)}}  "
            f"{item.section_path or '—'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
