"""Run M9d's detection funnel over the corpus, landing everything as `proposed`.

The backfill ADR-0033 describes. Nothing it writes is visible to a reader of the knowledge base:
every supersession lands `proposed` with a `clause_review` task beside it, and the other three
verdicts land in `clause_pair_verdicts`, which no serving path reads. Confirming is a person's
act and this script never performs one.

Two things make it safe to run more than once, which matters because the first run over 3,000
documents will be wrong in ways only its output can reveal:

* **the prompt cache** (ADR-0035) means a re-run pays for no verdict it has already paid for;
* **`clause_pair_verdicts`** means a re-run does not even assemble a pair it has concluded
  about — unless a clause's text has changed since, in which case it re-adjudicates, because a
  stored verdict is only good for the text it was made about.

Ordered by demand. `audit_log` has recorded every chunk id returned by every query since M2, so
"which of these clauses is anybody actually being served" is answerable, and the clauses being
served are the ones doing real harm. Where the log is empty the order is arbitrary and the
report says so rather than implying a ranking that does not exist.

    python scripts/detect_clause_supersessions.py                 # everything, demand-ordered
    python scripts/detect_clause_supersessions.py --document <id> # one document
    python scripts/detect_clause_supersessions.py --limit 50      # bounded chunk, resumable
    python scripts/detect_clause_supersessions.py --dry-run       # adjudicate, write nothing
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from kb_common.config import get_settings
from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from kb_ports.adapters.generation import OpenAiCompatibleGeneration
from kb_ports.adapters.generation_cached import CachedGeneration
from kb_registry.adjudicate import PROMPT_VERSION, ClauseAdjudicator
from kb_registry.demand import DemandReport, clause_demand
from kb_registry.funnel import ClauseFunnel
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Documents with at least one live chunk carrying a subject key. A chunk with neither subject
#: key nor embedding is invisible to gate 1, so a document made only of those has nothing to
#: detect and is not worth a transaction.
_DOCUMENTS = """
    SELECT DISTINCT c.document_id
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE NOT c.tombstoned
      AND c.section_path IS NOT NULL
      AND (c.subject_key IS NOT NULL OR c.embedding IS NOT NULL)
      AND d.status = 'published'
    ORDER BY c.document_id
"""


def build_adjudicator(session_factory: Callable[[], Session]) -> ClauseAdjudicator:
    """Gate 5, behind the cache.

    The cache is composed here rather than inside the adjudicator because ADR-0035 made it a
    wrapper around *any* `GenerationPort` — the adjudicator does not know it is being cached,
    and a caller that wants uncached verdicts simply does not wrap.
    """
    return ClauseAdjudicator(
        CachedGeneration(
            OpenAiCompatibleGeneration(hosted=False),
            session_factory,
            prompt_version=PROMPT_VERSION,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", help="one document id; default is every published document")
    parser.add_argument(
        "--limit", type=int, default=None, help="maximum pairs per document, for a bounded run"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="adjudicate and report, then roll back — costs model calls, writes nothing",
    )
    parser.add_argument("--demand-days", type=int, default=90, help="retrieval window for ranking")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("detect-clause-supersessions", settings.log_level, settings.log_format)
    engine = create_db_engine(settings.db)

    def session_factory() -> Session:
        return Session(engine)

    adjudicator = build_adjudicator(session_factory)

    with Session(engine) as session:
        demand = _demand(session, args.demand_days)
        documents = (
            [uuid.UUID(args.document)]
            if args.document
            else [row.document_id for row in session.execute(text(_DOCUMENTS))]
        )

        funnel = ClauseFunnel(session, adjudicator)
        totals = dict.fromkeys(("pairs", "by_gate", "by_model", "unresolved", "proposals"), 0)
        for document_id in documents:
            report = funnel.run(document_id, demand=demand, limit=args.limit)
            totals["pairs"] += report.pairs
            totals["by_gate"] += report.by_gate
            totals["by_model"] += report.by_model
            totals["unresolved"] += report.unresolved
            totals["proposals"] += report.proposals

        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    print(f"documents examined: {len(documents)}")
    print(f"pairs adjudicated:  {totals['pairs']}")
    print(f"  settled by gates: {totals['by_gate']}")
    print(f"  settled by model: {totals['by_model']}")
    print(f"  unresolved:       {totals['unresolved']} (retried on the next run)")
    print(f"proposals opened:   {totals['proposals']}")
    if args.dry_run:
        print("dry run: rolled back, nothing was written")
    else:
        print("every proposal is `proposed`; nothing is flagged until a steward confirms it")
    return 0


def _demand(session: Session, days: int) -> DemandReport | None:
    """The queue order, or an honest refusal to pretend there is one.

    An empty audit log and a corpus nobody is serving stale clauses from look identical in the
    output and mean opposite things, so the distinction is printed rather than smoothed over.
    """

    report = clause_demand(session, since=datetime.now(UTC) - timedelta(days=days))
    if report.records == 0:
        print(f"no retrieval records in the last {days} days — order is arbitrary, not a ranking")
        return None
    print(
        f"ranking {len(report.clauses)} clauses by demand from {report.records} retrievals "
        f"({report.resolvable_share:.0%} of chunk ids still resolve)"
    )
    return report


if __name__ == "__main__":
    raise SystemExit(main())
