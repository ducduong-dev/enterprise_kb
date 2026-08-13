#!/usr/bin/env python
"""Backup/restore drill — proves the backup is restorable, not merely present (M7).

A backup nobody has restored is a file. This takes one, restores it into a scratch database,
and then asks the questions that decide whether the bank could actually resume work from it:

1. **Did every row come back?** Compared against the manifest the backup wrote at the time,
   table by table, so a mismatch names what was lost rather than reporting "counts differ".
2. **Is exactly one version canonical per document?** The partial unique index enforces this
   live (INV-5); a restore that broke it would put the registry into a state the running
   system cannot produce.
3. **Do the chunks match their canonical versions?** Serving chunks from a superseded version
   is the failure INV-6 exists to prevent, and a half-restored database is a way to reach it
   without anyone publishing anything.
4. **Does retrieval work, with the filter still applied?** The drill runs a real query as a
   real principal, and checks the canaries are still unreachable. A restored corpus that
   answers questions but has lost its ACL columns is the worst possible outcome, and the only
   way to find out is to ask it something.

    KB_DB_NAME=kb uv run python scripts/backup_drill.py --keep-restored

Exits non-zero on the first failed check. Runs against a live database, so it is a `make`
target and an integration test rather than part of the unit suite.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BACKUP = ROOT / "ops" / "backup" / "backup.sh"
RESTORE = ROOT / "ops" / "backup" / "restore.sh"

#: Tables whose counts must match the manifest exactly.
MANIFEST_KEYS = (
    "documents",
    "document_versions",
    "chunks",
    "canonical",
    "document_refs",
    "review_tasks",
    "audit_log",
)
CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")


@dataclass
class DrillResult:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))
        mark = "ok  " if passed else "FAIL"
        print(f"  [{mark}] {name}{f' — {detail}' if detail else ''}")

    @property
    def passed(self) -> bool:
        return all(passed for _name, passed, _detail in self.checks)


def env_for(database: str | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    if database:
        environment["KB_DB_NAME"] = database
    return environment


def run(command: list[str], *, database: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, env=env_for(database), capture_output=True, text=True, check=False
    )


def read_manifest(path: Path) -> dict[str, int]:
    manifest: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, count = line.partition(",")
        if name.strip():
            manifest[name.strip()] = int(count)
    return manifest


def counts_from(database: str) -> dict[str, int]:
    from kb_common.config import get_settings, reset_settings_cache
    from kb_common.db import create_db_engine
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    os.environ["KB_DB_NAME"] = database
    reset_settings_cache()
    engine = create_db_engine(get_settings().db)
    try:
        with Session(engine) as session:
            return {
                "documents": session.execute(text("SELECT count(*) FROM documents")).scalar() or 0,
                "document_versions": session.execute(
                    text("SELECT count(*) FROM document_versions")
                ).scalar()
                or 0,
                "chunks": session.execute(text("SELECT count(*) FROM chunks")).scalar() or 0,
                "canonical": session.execute(
                    text("SELECT count(*) FROM documents WHERE canonical_version_id IS NOT NULL")
                ).scalar()
                or 0,
                "document_refs": session.execute(
                    text("SELECT count(*) FROM document_refs")
                ).scalar()
                or 0,
                "review_tasks": session.execute(text("SELECT count(*) FROM review_tasks")).scalar()
                or 0,
                "audit_log": session.execute(text("SELECT count(*) FROM audit_log")).scalar() or 0,
            }
    finally:
        engine.dispose()


def check_integrity(database: str, result: DrillResult) -> None:
    from kb_common.config import get_settings, reset_settings_cache
    from kb_common.db import create_db_engine
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    os.environ["KB_DB_NAME"] = database
    reset_settings_cache()
    engine = create_db_engine(get_settings().db)
    try:
        with Session(engine) as session:
            multiple = session.execute(
                text(
                    """
                    SELECT count(*) FROM (
                        SELECT document_id FROM document_versions
                        WHERE is_canonical GROUP BY document_id HAVING count(*) > 1
                    ) s
                    """
                )
            ).scalar()
            result.record(
                "one canonical version per document (INV-5)",
                multiple == 0,
                f"{multiple} document(s) with more than one",
            )

            orphaned = session.execute(
                text(
                    """
                    SELECT count(*) FROM chunks c
                    LEFT JOIN document_versions v ON v.id = c.version_id
                    WHERE v.id IS NULL
                    """
                )
            ).scalar()
            result.record("no chunk without its version", orphaned == 0, f"{orphaned} orphaned")

            stale = session.execute(
                text(
                    """
                    SELECT count(*) FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE NOT c.tombstoned
                      AND d.canonical_version_id IS NOT NULL
                      AND c.version_id <> d.canonical_version_id
                    """
                )
            ).scalar()
            result.record(
                "live chunks belong to the canonical version (INV-6)",
                stale == 0,
                f"{stale} chunk(s) from a superseded version",
            )

            pointers = session.execute(
                text(
                    """
                    SELECT count(*) FROM documents d
                    LEFT JOIN document_versions v ON v.id = d.canonical_version_id
                    WHERE d.canonical_version_id IS NOT NULL AND v.id IS NULL
                    """
                )
            ).scalar()
            result.record("canonical pointers resolve", pointers == 0, f"{pointers} dangling")
    finally:
        engine.dispose()


def check_retrieval(database: str, result: DrillResult) -> None:
    """Ask the restored corpus a question, as a real principal, through the real funnel."""
    from kb_authz.fixtures import ALL_PRINCIPALS
    from kb_common.config import get_settings, reset_settings_cache
    from kb_common.db import create_db_engine
    from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
    from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
    from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
    from kb_ports.adapters.rerank import LexicalRerankAdapter
    from kb_retrieval_api.engine import RetrievalEngine
    from kb_schemas.api import RetrieveRequest
    from sqlalchemy.orm import Session

    os.environ["KB_DB_NAME"] = database
    reset_settings_cache()
    engine = create_db_engine(get_settings().db)
    try:
        with Session(engine) as session:
            funnel = RetrievalEngine(
                session,
                keyword_index=PostgresFtsIndexAdapter(session),
                vector_index=PgVectorIndexAdapter(session),
                embedder=HashedEmbeddingAdapter(),
                reranker=LexicalRerankAdapter(),
            )
            response = funnel.retrieve(
                ALL_PRINCIPALS["user_retail_staff"],
                RetrieveRequest(query="tỷ lệ an toàn vốn tối thiểu", top_k=10),
            ).response
            result.record(
                "retrieval answers after the restore",
                bool(response.chunks),
                f"{len(response.chunks)} chunk(s)",
            )

            leaked: list[str] = []
            for principal_name in ("user_retail_staff", "user_it_engineer", "external_bot"):
                for query in ("CANARY", "sáp nhập", "Hội đồng quản trị"):
                    hits = funnel.retrieve(
                        ALL_PRINCIPALS[principal_name], RetrieveRequest(query=query, top_k=20)
                    ).response
                    for chunk in hits.chunks:
                        if any(token in chunk.text for token in CANARY_TOKENS):
                            leaked.append(f"{principal_name}:{query}")
            result.record(
                "the ACL filter survived the restore (INV-2)",
                not leaked,
                f"leaks: {leaked}" if leaked else "no canary reachable",
            )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir", type=Path, help="reuse an existing backup instead of taking one"
    )
    parser.add_argument("--target-db", default=f"kb_drill_{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--keep-restored", action="store_true", help="do not drop the scratch database"
    )
    args = parser.parse_args()

    source_db = os.environ.get("KB_DB_NAME", "kb")
    result = DrillResult()

    with tempfile.TemporaryDirectory(prefix="kb-drill-") as workspace:
        if args.backup_dir:
            backup_dir = args.backup_dir
            print(f"using existing backup {backup_dir}")
        else:
            print(f"taking a backup of {source_db}")
            taken = run(["bash", str(BACKUP), workspace], database=source_db)
            if taken.returncode != 0:
                print(taken.stdout + taken.stderr, file=sys.stderr)
                result.record("backup", False, "backup.sh failed")
                return 1
            directories = sorted(Path(workspace).glob("*/"))
            backup_dir = directories[-1]
            result.record("backup taken", True, str(backup_dir.name))

        manifest_path = backup_dir / "manifest.csv"
        manifest = read_manifest(manifest_path) if manifest_path.exists() else {}
        result.record("manifest written", bool(manifest), f"{len(manifest)} counters")

        print(f"restoring into {args.target_db}")
        restored = run(["bash", str(RESTORE), str(backup_dir), args.target_db], database=source_db)
        if restored.returncode != 0:
            print(restored.stdout + restored.stderr, file=sys.stderr)
            result.record("restore", False, "restore.sh failed")
            return 1
        result.record("restore completed", True)

        counts = counts_from(args.target_db)
        for key in MANIFEST_KEYS:
            if key not in manifest:
                continue
            result.record(
                f"{key} rows restored",
                counts.get(key, -1) == manifest[key],
                f"backup={manifest[key]} restored={counts.get(key)}",
            )

        check_integrity(args.target_db, result)
        check_retrieval(args.target_db, result)

        if not args.keep_restored:
            os.environ["KB_DB_NAME"] = source_db
            container = os.environ.get("KB_PG_DOCKER_CONTAINER")
            drop_sql = f"DROP DATABASE IF EXISTS {args.target_db}"
            user = os.environ.get("KB_DB_USER", "kb")
            if container:
                drop = run(
                    [
                        "docker",
                        "exec",
                        "-e",
                        "PGPASSWORD",
                        "-i",
                        container,
                        "psql",
                        "--username",
                        user,
                        "--dbname",
                        "postgres",
                        "--quiet",
                        "--command",
                        drop_sql,
                    ]
                )
            else:
                drop = run(
                    [
                        "psql",
                        "--host",
                        os.environ.get("KB_DB_HOST", "localhost"),
                        "--port",
                        os.environ.get("KB_DB_PORT", "5432"),
                        "--username",
                        user,
                        "--dbname",
                        "postgres",
                        "--quiet",
                        "--command",
                        drop_sql,
                    ]
                )
            if drop.returncode != 0:  # pragma: no cover - cleanup is best effort
                print(f"  note: could not drop {args.target_db}: {drop.stderr.strip()}")

    os.environ["KB_DB_NAME"] = source_db
    failed = [name for name, passed, _ in result.checks if not passed]
    if failed:
        print(f"\nDRILL FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\ndrill passed — {len(result.checks)} checks")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
