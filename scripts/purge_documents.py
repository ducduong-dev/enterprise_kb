"""Remove documents from the registry, through the guard rather than around it.

Emptying the registry is an ordinary operational need on a dev machine — clearing fixtures out
to make room for real ones — and a genuinely dangerous one everywhere else. So this is a script
with an explicit confirmation rather than a Makefile target that a tab-complete can fire.

**It goes through the purge path production uses (INV-9).** `document_versions` is protected by
a database trigger that refuses a delete unless `kb.purge_authorized` is set for the
transaction *and* the retention hold has passed; this clears the hold explicitly and sets the
flag, so the guard is exercised rather than bypassed, and the whole thing is one transaction
that leaves an audit record behind. A purge nobody can account for afterwards is the failure
INV-9 exists to prevent.

Categories are never touched. Every upload requires a valid `category_path`, so an empty
`categories` table is a portal that accepts nothing — and the categories are the policy surface
(ADR-0003), not content.

    python scripts/purge_documents.py                      # show what would go
    python scripts/purge_documents.py --yes                # do it
    python scripts/purge_documents.py --category t_ --yes  # only one subtree
"""

from __future__ import annotations

import argparse
import json

from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Deleted in foreign-key order. `document_expiry`, `clause_supersessions`,
#: `document_declarations` and `pending_document_refs` are absent on purpose: all four cascade
#: from `documents`, and listing them here as well would be a second place to keep in step.
_ORDER = (
    ("chunks", "document_id IN ({docs})"),
    (
        "review_tasks",
        "version_id IN (SELECT id FROM document_versions WHERE document_id IN ({docs}))",
    ),
    ("graph_serving", "src_document_id IN ({docs}) OR dst_document_id IN ({docs})"),
    ("document_refs", "src_document_id IN ({docs}) OR dst_document_id IN ({docs})"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category",
        default="",
        help="only documents whose category path starts with this (default: every document)",
    )
    parser.add_argument("--yes", action="store_true", help="actually delete")
    args = parser.parse_args()

    configure_logging("purge-documents", "INFO", "text")
    scope = "SELECT id FROM documents"
    params: dict[str, object] = {}
    if args.category:
        scope += " WHERE category_path::text LIKE :prefix"
        params["prefix"] = f"{args.category}%"

    with Session(create_db_engine()) as session:
        listing = session.execute(
            text(
                f"SELECT title, category_path::text AS category, status FROM documents "
                f"WHERE id IN ({scope}) ORDER BY category_path, title"
            ),
            params,
        ).all()
        chunks = session.execute(
            text(f"SELECT count(*) FROM chunks WHERE document_id IN ({scope})"), params
        ).scalar()

        if not listing:
            print("Nothing matches.")
            return 0

        print(f"{len(listing)} documents, {chunks} chunks:")
        for row in listing:
            print(f"  {row.status:<10} {row.category:<26} {row.title[:56]}")

        if not args.yes:
            print("\nNothing deleted. Re-run with --yes to proceed.")
            return 0

        # One transaction: a half-purged registry has documents pointing at versions that are
        # gone, which every read path would then have to defend against.
        session.execute(text("SET LOCAL kb.purge_authorized = 'on'"))
        session.execute(
            text(
                f"UPDATE document_versions SET retention_until = DATE '2000-01-01' "
                f"WHERE document_id IN ({scope})"
            ),
            params,
        )
        for table, where in _ORDER:
            session.execute(text(f"DELETE FROM {table} WHERE {where.format(docs=scope)}"), params)
        session.execute(
            text(f"UPDATE documents SET canonical_version_id = NULL WHERE id IN ({scope})"),
            params,
        )
        session.execute(
            text(f"DELETE FROM document_versions WHERE document_id IN ({scope})"), params
        )
        session.execute(text(f"DELETE FROM documents WHERE id IN ({scope})"), params)

        session.execute(
            text(
                "INSERT INTO audit_log (ts, actor, action, object_ref, detail) VALUES "
                "(now(), 'operator', 'purge_attempt', CAST(:ref AS jsonb), CAST(:detail AS jsonb))"
            ),
            {
                "ref": json.dumps({"scope": args.category or "all_documents"}),
                "detail": json.dumps(
                    {
                        "documents": len(listing),
                        "chunks": int(chunks or 0),
                        "titles": [row.title for row in listing][:50],
                    },
                    ensure_ascii=False,
                ),
            },
        )
        session.commit()

    print(f"\nPurged {len(listing)} documents and {chunks} chunks. Categories untouched.")
    print("`make seed` restores the fixture corpus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
