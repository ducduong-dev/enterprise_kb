#!/usr/bin/env python
"""Bake-off runner — OpenSearch vs pg_search ([OPEN]-2, M7).

Executes `eval/bakeoff/protocol.md`: five gate items evaluated first, then the scored
comparison, over one corpus loaded once. An engine that fails a gate is not scored — the
protocol says Vietnamese tokenization is the decider, so a fast engine that cannot find
"an toan von" is not a candidate whose latency is interesting.

Both engines answer from the same rows. `pg_search` reads the `chunks` table directly;
OpenSearch is loaded from that same table through the indexer's own mapping, so a difference
in the results is a difference in the engine and not in what it was given.

    uv run python eval/bakeoff/run.py --engines pg_search,postgres_fts

**The decision is made** (ADR-0021: pg_search), and the OpenSearch adapter is deleted, so
`--engines opensearch` now reports it as unavailable. This runner stays because the ADR names
the conditions that would reopen the question — chiefly a corpus an order of magnitude larger
— and re-running the comparison then should mean restoring one adapter, not rebuilding the
harness.

Requirements the runner checks rather than assumes: a Postgres with the corpus (and, for
pg_search, `ops/pg_search/install.sql` applied), and a reachable OpenSearch. Anything missing
is reported as *unavailable* rather than silently skipped — a comparison table with a column
quietly missing is worse than no table.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval" / "harness"))

#: The plan's latency budget for the funnel. Keyword search is one leg of it, so it gets a
#: tighter target than the hybrid pipeline it feeds.
KEYWORD_P95_MS = 150.0
HYBRID_P95_MS = 800.0
#: Concurrency the protocol asks for.
CONCURRENCY = 50
#: Marks a chunk this runner invented to make the table big enough for latency to mean
#: something. Also how it finds them again: a previous run's copies left in place would be
#: measured as quality, and quality on duplicated text is a count of copies.
SYNTHETIC_MARKER = "[bản sao thử tải"


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class EngineReport:
    engine: str
    available: bool
    unavailable_reason: str = ""
    gates: list[GateResult] = field(default_factory=list)
    recall: dict[str, float] = field(default_factory=dict)
    ndcg: dict[str, float] = field(default_factory=dict)
    freshness_ms: float = 0.0
    keyword_p50_ms: float = 0.0
    keyword_p95_ms: float = 0.0
    hybrid_p50_ms: float = 0.0
    hybrid_p95_ms: float = 0.0
    errors: int = 0
    corpus_chunks: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def gate_passed(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates)


# --------------------------------------------------------------------------------- engines


def build_engine_adapter(name: str, session: Any) -> Any:
    if name == "pg_search":
        from kb_ports.adapters.pg_search_index import PgSearchIndexAdapter

        return PgSearchIndexAdapter(session)
    if name == "opensearch":
        from kb_ports.adapters.opensearch_index import OpenSearchIndexAdapter

        return OpenSearchIndexAdapter()
    if name == "postgres_fts":
        from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter

        return PostgresFtsIndexAdapter(session)
    raise SystemExit(f"unknown engine {name!r}")


def _index_documents(session: Any, *, chunk_ids: list[Any] | None = None) -> list[Any]:
    """Canonical chunks as the indexer maps them. Optionally just the ones named."""
    from kb_ports.indexes import IndexDocument
    from sqlalchemy import text

    scope = "AND c.id = ANY(CAST(:ids AS uuid[]))" if chunk_ids else ""
    rows = (
        session.execute(
            text(
                f"""
                SELECT c.id, c.document_id, c.version_id, c.text, c.citation_label,
                       c.section_path, c.visibility, c.allowed_groups, c.department,
                       c.category_path::text AS category_path, c.doc_class, c.doc_status,
                       c.effective_from, c.effective_to, c.tombstoned, c.ordinal, c.page,
                       d.legal_number
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE NOT c.tombstoned {scope}
                ORDER BY c.ordinal
                """
            ),
            {"ids": [str(chunk_id) for chunk_id in chunk_ids]} if chunk_ids else {},
        )
        .mappings()
        .all()
    )
    return [
        IndexDocument(
            chunk_id=row["id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            text=row["text"],
            citation_label=row["citation_label"],
            section_path=row["section_path"],
            visibility=row["visibility"],
            allowed_groups=list(row["allowed_groups"] or []),
            department=row["department"],
            doc_status=row["doc_status"],
            doc_class=row["doc_class"],
            category_path=row["category_path"],
            effective_from=row["effective_from"].isoformat() if row["effective_from"] else None,
            effective_to=row["effective_to"].isoformat() if row["effective_to"] else None,
            tombstoned=row["tombstoned"],
            extra={
                "ordinal": row["ordinal"],
                "page": row["page"],
                "legal_number": row["legal_number"],
            },
        )
        for row in rows
    ]


def load_opensearch(session: Any, adapter: Any) -> int:
    """Mirror every canonical chunk into OpenSearch, exactly as the indexer would.

    The index is dropped first. `upsert` is an upsert, so a previous run's documents would
    survive it — and a quality score measured against a corpus the runner did not put there
    is not a measurement of anything.
    """

    with suppress(Exception):
        adapter._client.indices.delete(index=adapter._index)
    adapter.ensure_index()
    documents = _index_documents(session)
    adapter.upsert(documents)
    return len(documents)


# ----------------------------------------------------------------------------------- gates


def run_gates(engine: str, adapter: Any, session: Any, keys: dict[str, str]) -> list[GateResult]:
    from kb_authz.filters import FilterBuilder
    from kb_authz.fixtures import ALL_PRINCIPALS

    acl = FilterBuilder().base(ALL_PRINCIPALS["user_retail_staff"])

    def docs(query: str, top_k: int = 20) -> list[str]:
        return [
            keys.get(str(hit.document_id), str(hit.document_id))
            for hit in adapter.search(query, acl, top_k=top_k)
        ]

    gates: list[GateResult] = []

    # 1. Diacritics, both directions.
    accented = docs("tỷ lệ an toàn vốn")
    folded = docs("ty le an toan von")
    ok = bool(accented) and "tt41-capital" in accented and "tt41-capital" in folded
    gates.append(
        GateResult(
            "diacritics",
            ok,
            f"accented={accented[:3]} folded={folded[:3]}",
        )
    )

    # 2. Compound terms outrank a single word.
    compound = docs("tỷ lệ an toàn vốn")
    gates.append(
        GateResult(
            "compound terms",
            bool(compound) and compound[0] == "tt41-capital",
            f"top={compound[:3]}",
        )
    )

    # 3. Legal numbers, in the three shapes people type.
    forms = {form: docs(form) for form in ("41/2016/TT-NHNN", "TT41", "Thông tư 41")}
    reached = {form: "tt41-capital" in found for form, found in forms.items()}
    gates.append(
        GateResult(
            "legal numbers",
            all(reached.values()),
            ", ".join(f"{form}={'hit' if hit else 'MISS'}" for form, hit in reached.items()),
        )
    )

    # 4. A bilingual document, from either language.
    vi = docs("quản lý rủi ro tính toán tỷ lệ an toàn vốn hằng tháng")
    en = docs("internal buffer above the regulatory minimum")
    gates.append(
        GateResult(
            "mixed language",
            "policy-capital-internal" in vi and "policy-capital-internal" in en,
            f"vi={'hit' if 'policy-capital-internal' in vi else 'MISS'}, "
            f"en={'hit' if 'policy-capital-internal' in en else 'MISS'}",
        )
    )

    # 5. The ACL filter, applied inside the query. Two pieces of evidence: no canary reaches
    #    any principal, and — where the engine can show it — the plan itself.
    canary_tokens = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")
    leaked: list[str] = []
    for principal_name in ("user_retail_staff", "user_it_engineer", "external_bot"):
        principal_acl = FilterBuilder().base(ALL_PRINCIPALS[principal_name])
        for query in ("CANARY", "sáp nhập", "Hội đồng quản trị"):
            for hit in adapter.search(query, principal_acl, top_k=50):
                if any(token in (hit.text or "") for token in canary_tokens):
                    leaked.append(f"{principal_name}:{query}")
    evidence = ""
    if hasattr(adapter, "explain"):
        plan = adapter.explain("tỷ lệ an toàn vốn", acl)
        first = next((line.strip() for line in plan.splitlines() if line.strip()), "")
        evidence = f"; plan={first[:80]}"
    gates.append(GateResult("acl in query", not leaked, f"leaks={leaked or 'none'}{evidence}"))

    return gates


# ---------------------------------------------------------------------------------- scores


def score_quality(adapter: Any, session: Any, keys: dict[str, str], report: EngineReport) -> None:
    """recall@10 and nDCG@10 over the golden set, keyword-only, split by query language."""
    from kb_authz.filters import FilterBuilder
    from kb_authz.fixtures import ALL_PRINCIPALS
    from metrics import ndcg_at_k, recall_at_k
    from run import load_golden

    per_language: dict[str, list[tuple[float, float]]] = {}
    for entry in load_golden(ROOT / "eval" / "golden_set"):
        acl = FilterBuilder().base(ALL_PRINCIPALS[entry["principal"]])
        retrieved: list[str] = []
        for hit in adapter.search(entry["query"], acl, top_k=10):
            key = keys.get(str(hit.document_id), str(hit.document_id))
            if key not in retrieved:
                retrieved.append(key)
        grades = {item["doc"]: int(item["grade"]) for item in entry.get("relevant") or []}
        relevant = [doc for doc, grade in grades.items() if grade > 0]
        language = str(entry.get("lang", "vi"))
        per_language.setdefault(language, []).append(
            (recall_at_k(retrieved, relevant, 10), ndcg_at_k(retrieved, grades, 10))
        )

    for language, pairs in per_language.items():
        report.recall[language] = sum(p[0] for p in pairs) / len(pairs)
        report.ndcg[language] = sum(p[1] for p in pairs) / len(pairs)
    flat = [pair for pairs in per_language.values() for pair in pairs]
    report.recall["all"] = sum(p[0] for p in flat) / len(flat)
    report.ndcg["all"] = sum(p[1] for p in flat) / len(flat)


def measure_freshness(engine: str, adapter: Any, session: Any) -> float:
    """Publish-to-searchable, in milliseconds, for one chunk.

    The protocol's freshness dimension against the 10 s budget (INV-5). For a Postgres engine
    the answer is "as soon as the transaction commits", and the measurement says so rather
    than asserting it. For OpenSearch it is the write plus the refresh — the *end-to-end*
    number additionally includes outbox polling, which `tests/test_publish_consistency.py`
    holds to the same budget.
    """
    from kb_authz.filters import FilterBuilder
    from kb_authz.fixtures import ALL_PRINCIPALS
    from sqlalchemy import text

    token = f"FRESHNESSPROBE{uuid.uuid4().hex[:8].upper()}"
    acl = FilterBuilder().base(ALL_PRINCIPALS["user_retail_staff"])
    row = session.execute(
        text(
            """
            INSERT INTO chunks (
                id, document_id, version_id, ordinal, text, citation_label, section_path,
                page, visibility, allowed_groups, department, category_path, doc_class,
                doc_status, effective_from, effective_to, tombstoned, embedding
            )
            SELECT gen_random_uuid(), c.document_id, c.version_id, 9_000_000,
                   :token || ' ' || c.text, c.citation_label, c.section_path, c.page,
                   c.visibility, c.allowed_groups, c.department, c.category_path,
                   c.doc_class, c.doc_status, c.effective_from, c.effective_to,
                   c.tombstoned, c.embedding
            FROM chunks c
            WHERE NOT c.tombstoned AND c.visibility = 'internal_all'
            LIMIT 1
            RETURNING id
            """
        ),
        {"token": token},
    ).scalar()
    session.commit()

    started = time.perf_counter()
    if engine == "opensearch":
        # One document, the way the indexer writes one — reloading the corpus would measure
        # a backfill and call it freshness.
        adapter.upsert(_index_documents(session, chunk_ids=[row]))
    deadline = started + 15.0
    elapsed = 0.0
    while time.perf_counter() < deadline:
        if any(token in (hit.text or "") for hit in adapter.search(token, acl, top_k=10)):
            elapsed = time.perf_counter() - started
            break
        time.sleep(0.05)
    else:  # pragma: no cover - a 15 s miss is a failure worth seeing in the table
        elapsed = float("inf")

    session.execute(text("DELETE FROM chunks WHERE id = :id"), {"id": row})
    session.commit()
    return elapsed * 1000


def percentiles(samples: list[float]) -> tuple[float, float]:
    if not samples:
        return 0.0, 0.0
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return p50 * 1000, p95 * 1000


def score_latency(
    engine: str,
    session_factory: Any,
    keys: dict[str, str],
    report: EngineReport,
    *,
    concurrency: int,
    rounds: int,
) -> None:
    """Keyword-only and full-hybrid latency at the protocol's concurrency.

    Each worker gets its own session: a shared SQLAlchemy session is not thread-safe, and
    measuring contention on a lock we invented would measure nothing about the engine.
    """
    from kb_authz.filters import FilterBuilder
    from kb_authz.fixtures import ALL_PRINCIPALS
    from run import load_golden

    queries = [entry["query"] for entry in load_golden(ROOT / "eval" / "golden_set")]
    principal = ALL_PRINCIPALS["user_retail_staff"]
    acl = FilterBuilder().base(principal)

    keyword_samples: list[float] = []
    hybrid_samples: list[float] = []
    errors = 0

    def keyword_worker(index: int) -> list[float]:
        nonlocal errors
        samples: list[float] = []
        # OpenSearch needs no database connection; opening one anyway would charge it for
        # Postgres pool contention it never causes.
        from contextlib import nullcontext

        context = nullcontext(None) if engine == "opensearch" else session_factory()
        with context as session:
            adapter = build_engine_adapter(engine, session)
            for round_index in range(rounds):
                query = queries[(index + round_index) % len(queries)]
                started = time.perf_counter()
                try:
                    adapter.search(query, acl, top_k=50)
                except Exception:  # pragma: no cover - engine failure is a result
                    errors += 1
                    continue
                samples.append(time.perf_counter() - started)
        return samples

    def hybrid_worker(index: int) -> list[float]:
        nonlocal errors
        from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
        from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
        from kb_ports.adapters.rerank import LexicalRerankAdapter
        from kb_retrieval_api.engine import RetrievalEngine
        from kb_schemas.api import RetrieveRequest

        samples: list[float] = []
        with session_factory() as session:
            funnel = RetrievalEngine(
                session,
                keyword_index=build_engine_adapter(engine, session),
                vector_index=PgVectorIndexAdapter(session),
                embedder=HashedEmbeddingAdapter(),
                reranker=LexicalRerankAdapter(),
            )
            for round_index in range(rounds):
                query = queries[(index + round_index) % len(queries)]
                started = time.perf_counter()
                try:
                    funnel.retrieve(principal, RetrieveRequest(query=query, top_k=10))
                except Exception:  # pragma: no cover - engine failure is a result
                    errors += 1
                    continue
                samples.append(time.perf_counter() - started)
        return samples

    for worker, sink in ((keyword_worker, keyword_samples), (hybrid_worker, hybrid_samples)):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for samples in pool.map(worker, range(concurrency)):
                sink.extend(samples)

    report.keyword_p50_ms, report.keyword_p95_ms = percentiles(keyword_samples)
    report.hybrid_p50_ms, report.hybrid_p95_ms = percentiles(hybrid_samples)
    report.errors = errors


# --------------------------------------------------------------------------------- corpus


def drop_synthetic_chunks(session: Any) -> int:
    """Remove the copies a previous run left behind, before anything is measured.

    Followed by a reindex where the BM25 index exists. Deleting thousands of rows underneath
    it once left `paradedb.score()` raising `item_pointer_is_valid(ctid)` on every query until
    the index was rebuilt (ADR-0021, operational notes) — a state a benchmark must not leave a
    developer's database in.
    """
    from sqlalchemy import text

    removed = session.execute(
        text("DELETE FROM chunks WHERE text LIKE :marker"),
        {"marker": f"%{SYNTHETIC_MARKER}%"},
    ).rowcount
    session.commit()
    if removed:
        indexed = session.execute(
            text("SELECT count(*) FROM pg_class WHERE relname = 'chunks_bm25'")
        ).scalar()
        if indexed:
            session.execute(text("REINDEX INDEX chunks_bm25"))
            session.commit()
    return int(removed)


def scale_corpus(session: Any, factor: int) -> int:
    """Duplicate the seeded chunks into synthetic documents, to give latency something to do.

    Honest about what it is: the text is derived from the real corpus, so absolute quality
    numbers on a scaled corpus mean nothing and the runner does not compute them. What scaling
    measures is how each engine behaves as the table grows, which is the question the protocol
    asks about a 3,000-document backfill nobody has yet loaded.
    """
    from sqlalchemy import text

    if factor <= 1:
        return 0
    inserted = session.execute(
        text(
            """
            INSERT INTO chunks (
                id, document_id, version_id, ordinal, text, citation_label, section_path,
                page, visibility, allowed_groups, department, category_path, doc_class,
                doc_status, effective_from, effective_to, tombstoned, embedding
            )
            SELECT gen_random_uuid(), c.document_id, c.version_id,
                   c.ordinal + (g.i * 1000),
                   c.text || ' ' || :marker || ' ' || g.i || ']',
                   c.citation_label, c.section_path, c.page, c.visibility, c.allowed_groups,
                   c.department, c.category_path, c.doc_class, c.doc_status,
                   c.effective_from, c.effective_to, c.tombstoned, c.embedding
            FROM chunks c CROSS JOIN generate_series(1, :factor) AS g(i)
            WHERE NOT c.tombstoned
            """
        ),
        {"factor": factor - 1, "marker": SYNTHETIC_MARKER},
    ).rowcount
    session.commit()
    return int(inserted)


# -------------------------------------------------------------------------------- reporting


def render(reports: list[EngineReport], *, corpus_chunks: int, concurrency: int) -> None:
    print(f"\ncorpus: {corpus_chunks} chunks · concurrency: {concurrency}\n")

    print("GATES (protocol §Gate — an engine failing any item is out)")
    names = [gate.name for report in reports if report.available for gate in report.gates]
    ordered = list(dict.fromkeys(names))
    header = f"{'gate':<18}" + "".join(f"{report.engine:>14}" for report in reports)
    print(header)
    for name in ordered:
        row = f"{name:<18}"
        for report in reports:
            gate = next((g for g in report.gates if g.name == name), None)
            row += f"{('pass' if gate.passed else 'FAIL') if gate else 'n/a':>14}"
        print(row)
    for report in reports:
        for gate in report.gates:
            if not gate.passed:
                print(f"  {report.engine}/{gate.name}: {gate.detail}")

    print("\nSCORES")
    metrics: list[tuple[str, Any]] = [
        ("recall@10 (all)", lambda r: f"{r.recall.get('all', 0):.3f}"),
        ("recall@10 (vi)", lambda r: f"{r.recall.get('vi', 0):.3f}"),
        ("recall@10 (en)", lambda r: f"{r.recall.get('en', 0):.3f}"),
        ("nDCG@10 (all)", lambda r: f"{r.ndcg.get('all', 0):.3f}"),
        ("freshness ms", lambda r: f"{r.freshness_ms:.0f}"),
        ("keyword p50 ms", lambda r: f"{r.keyword_p50_ms:.1f}"),
        ("keyword p95 ms", lambda r: f"{r.keyword_p95_ms:.1f}"),
        ("hybrid p50 ms", lambda r: f"{r.hybrid_p50_ms:.1f}"),
        ("hybrid p95 ms", lambda r: f"{r.hybrid_p95_ms:.1f}"),
        ("errors", lambda r: str(r.errors)),
    ]
    print(f"{'metric':<18}" + "".join(f"{report.engine:>14}" for report in reports))
    for label, fn in metrics:
        row = f"{label:<18}"
        for report in reports:
            row += f"{(fn(report) if report.gate_passed else '—'):>14}"
        print(row)

    print("\nTARGETS")
    for report in reports:
        if not report.available:
            print(f"  {report.engine}: unavailable — {report.unavailable_reason}")
            continue
        if not report.gate_passed:
            print(f"  {report.engine}: failed the gate; not scored")
            continue
        keyword_ok = report.keyword_p95_ms <= KEYWORD_P95_MS
        hybrid_ok = report.hybrid_p95_ms <= HYBRID_P95_MS
        print(
            f"  {report.engine}: keyword p95 {report.keyword_p95_ms:.1f} ms "
            f"({'meets' if keyword_ok else 'MISSES'} {KEYWORD_P95_MS:.0f} ms), "
            f"hybrid p95 {report.hybrid_p95_ms:.1f} ms "
            f"({'meets' if hybrid_ok else 'MISSES'} {HYBRID_P95_MS:.0f} ms)"
        )
        for note in report.notes:
            print(f"    note: {note}")


# ------------------------------------------------------------------------------------ main


def evaluate_quality(engine: str, session_factory: Any, keys: dict[str, str]) -> EngineReport:
    """Gates and quality, on the real corpus. Never on a scaled one: duplicated text would
    make recall a measure of how many copies of the answer exist."""
    report = EngineReport(engine=engine, available=False)
    with session_factory() as session:
        try:
            adapter = build_engine_adapter(engine, session)
        except Exception as exc:
            report.unavailable_reason = f"adapter could not be built: {exc}"
            return report
        try:
            healthy = adapter.health()
        except Exception as exc:
            report.unavailable_reason = f"health check failed: {exc}"
            return report
        if not healthy:
            report.unavailable_reason = "engine reports itself unhealthy or not installed"
            return report

        report.available = True
        if engine == "opensearch":
            loaded = load_opensearch(session, adapter)
            report.notes.append(f"loaded {loaded} chunks into the index")

        from sqlalchemy import text as sql_text

        report.corpus_chunks = int(
            session.execute(sql_text("SELECT count(*) FROM chunks WHERE NOT tombstoned")).scalar()
            or 0
        )
        report.gates = run_gates(engine, adapter, session, keys)
        if not report.gate_passed:
            return report
        score_quality(adapter, session, keys, report)
        report.freshness_ms = measure_freshness(engine, adapter, session)
    return report


def measurement_engine(concurrency: int) -> Any:
    """A database engine sized for the run.

    The service default is a pool of 10 (+5 overflow). Measuring 50 concurrent searches
    through it would measure the pool, not the engine — every sample past the fifteenth
    includes time spent waiting for a connection. Production needs the same treatment: a
    pool or a pgbouncer sized to real concurrency, which is a finding of this exercise
    rather than a detail of it.
    """
    from kb_common.config import get_settings
    from sqlalchemy import create_engine

    cfg = get_settings().db
    return create_engine(cfg.url, pool_size=concurrency, max_overflow=0, pool_pre_ping=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", default="pg_search,postgres_fts")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--rounds", type=int, default=10, help="queries per worker")
    parser.add_argument(
        "--scale",
        type=int,
        default=1,
        help="duplicate the corpus this many times before measuring (latency only)",
    )
    parser.add_argument("--json", type=Path, help="write the raw report here")
    args = parser.parse_args()

    from contextlib import contextmanager

    from kb_common.config import get_settings
    from kb_common.db import create_db_engine
    from run import document_key_map
    from sqlalchemy.orm import Session

    db = create_db_engine(get_settings().db)
    measure_db = measurement_engine(args.concurrency)

    @contextmanager
    def session_factory() -> Any:
        with Session(db) as session:
            yield session

    @contextmanager
    def measure_session_factory() -> Any:
        with Session(measure_db) as session:
            yield session

    keys = document_key_map()
    corpus_chunks = 0
    try:
        engines = [name.strip() for name in args.engines.split(",") if name.strip()]

        with session_factory() as session:
            removed = drop_synthetic_chunks(session)
        if removed:
            print(f"removed {removed} synthetic chunks from an earlier run")

        # Phase 1: gates and quality, on the corpus as published.
        reports = [evaluate_quality(engine, session_factory, keys) for engine in engines]

        # Phase 2: latency, optionally on a scaled corpus. Scaling happens *after* quality so
        # the two measurements never contaminate each other.
        if args.scale > 1:
            with session_factory() as session:
                added = scale_corpus(session, args.scale)
                print(f"scaled the corpus by {args.scale}x (+{added} synthetic chunks)")
            for report in reports:
                if report.engine == "opensearch" and report.available:
                    with session_factory() as session:
                        loaded = load_opensearch(
                            session, build_engine_adapter("opensearch", session)
                        )
                        report.notes.append(f"reloaded {loaded} chunks after scaling")
        with session_factory() as session:
            from sqlalchemy import text as sql_text

            corpus_chunks = int(
                session.execute(
                    sql_text("SELECT count(*) FROM chunks WHERE NOT tombstoned")
                ).scalar()
                or 0
            )
        for report in reports:
            if report.available and report.gate_passed:
                report.corpus_chunks = corpus_chunks
                report.notes.append(f"measured through a pool of {args.concurrency} connections")
                score_latency(
                    report.engine,
                    measure_session_factory,
                    keys,
                    report,
                    concurrency=args.concurrency,
                    rounds=args.rounds,
                )
        render(reports, corpus_chunks=corpus_chunks, concurrency=args.concurrency)

        if args.json:
            args.json.write_text(
                json.dumps(
                    {
                        "corpus_chunks": corpus_chunks,
                        "concurrency": args.concurrency,
                        "run_id": str(uuid.uuid4()),
                        "engines": [
                            {
                                "engine": report.engine,
                                "available": report.available,
                                "unavailable_reason": report.unavailable_reason,
                                "gates": [vars(gate) for gate in report.gates],
                                "recall": report.recall,
                                "ndcg": report.ndcg,
                                "freshness_ms": report.freshness_ms,
                                "keyword_p50_ms": report.keyword_p50_ms,
                                "keyword_p95_ms": report.keyword_p95_ms,
                                "hybrid_p50_ms": report.hybrid_p50_ms,
                                "hybrid_p95_ms": report.hybrid_p95_ms,
                                "errors": report.errors,
                                "notes": report.notes,
                            }
                            for report in reports
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        unavailable = [report for report in reports if not report.available]
        if unavailable:
            print(
                "\nincomplete: "
                + ", ".join(f"{r.engine} ({r.unavailable_reason})" for r in unavailable),
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        # Leave the corpus as it was found: synthetic chunks left behind would be measured by
        # the next thing that runs, including the quality gates in CI.
        with suppress(Exception), Session(db) as session:
            drop_synthetic_chunks(session)
        db.dispose()
        measure_db.dispose()


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
