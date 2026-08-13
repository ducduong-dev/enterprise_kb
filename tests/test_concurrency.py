"""The funnel under concurrent load (M7).

`ops/loadtest/run.py` drives the running stack over HTTP and is where the real numbers come
from. This runs in CI, in process, and holds the two properties that must not regress quietly
between load tests:

* **Correctness under concurrency.** Fifty threads retrieving as different principals must get
  exactly the results they get alone. A filter that is right single-threaded and wrong under
  load is the worst kind of ACL bug, because it never reproduces on a developer's machine.
* **A latency ceiling with room in it.** CI machines are shared and slow, so the ceiling here
  is deliberately far above the production budget: it catches an algorithmic regression (an
  N+1 query, a lock, an unindexed scan), not a noisy neighbour.
"""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from kb_authz.fixtures import ALL_PRINCIPALS
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import RetrieveRequest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

#: The protocol's concurrency. Each worker holds its own session, so this is also the number
#: of database connections the run needs — which is the sizing lesson from the bake-off.
CONCURRENCY = 50
QUERIES_PER_WORKER = 4
#: Generous by design; the production budget is 800 ms p95 and lives in `ops/loadtest`.
CI_P95_CEILING_MS = 3000.0

PRINCIPALS = [
    "user_retail_staff",
    "user_branch_teller",
    "user_compliance_officer",
    "user_legal_counsel",
    "user_it_engineer",
]
QUERIES = [
    "tỷ lệ an toàn vốn tối thiểu",
    "hệ số rủi ro tín dụng",
    "phí duy trì tài khoản",
    "quy trình nhận biết khách hàng",
]
CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")


def funnel(session: Session) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
    )


def run_worker(engine: Engine, index: int) -> tuple[list[float], list[tuple[str, list[str]]]]:
    """One worker: its own connection, its own principal, its own session."""
    principal = ALL_PRINCIPALS[PRINCIPALS[index % len(PRINCIPALS)]]
    latencies: list[float] = []
    seen: list[tuple[str, list[str]]] = []
    with Session(engine) as session:
        retrieval = funnel(session)
        for step in range(QUERIES_PER_WORKER):
            query = QUERIES[(index + step) % len(QUERIES)]
            started = time.perf_counter()
            response = retrieval.retrieve(
                principal, RetrieveRequest(query=query, top_k=10)
            ).response
            latencies.append(time.perf_counter() - started)
            seen.append(
                (
                    f"{PRINCIPALS[index % len(PRINCIPALS)]}|{query}",
                    [str(chunk.chunk_id) for chunk in response.chunks],
                )
            )
    return latencies, seen


def test_the_funnel_returns_the_same_results_under_load(pristine_corpus: Engine) -> None:
    """Fifty concurrent readers, five principals, one answer per (principal, query)."""
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        outcomes = list(
            pool.map(lambda index: run_worker(pristine_corpus, index), range(CONCURRENCY))
        )

    by_key: dict[str, set[tuple[str, ...]]] = {}
    for _latencies, seen in outcomes:
        for key, chunk_ids in seen:
            by_key.setdefault(key, set()).add(tuple(chunk_ids))

    unstable = {key: results for key, results in by_key.items() if len(results) > 1}
    assert not unstable, f"concurrent retrieval returned different results for: {list(unstable)}"


@pytest.mark.acl_sweep
def test_no_canary_leaks_under_concurrency(pristine_corpus: Engine) -> None:
    """The ACL sweep, run in parallel. A filter built per request must not be affected by
    what other requests are doing."""

    def probe(index: int) -> list[str]:
        principal = ALL_PRINCIPALS[PRINCIPALS[index % len(PRINCIPALS)]]
        leaks: list[str] = []
        with Session(pristine_corpus) as session:
            retrieval = funnel(session)
            for query in ("CANARY", "sáp nhập", "Hội đồng quản trị", "tỷ lệ an toàn vốn"):
                response = retrieval.retrieve(
                    principal, RetrieveRequest(query=query, top_k=20)
                ).response
                for chunk in response.chunks:
                    if any(token in chunk.text for token in CANARY_TOKENS):
                        leaks.append(f"{principal.subject}:{query}")
        return leaks

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        leaks = [leak for batch in pool.map(probe, range(CONCURRENCY)) for leak in batch]
    assert not leaks, f"canary content reached a principal under load: {leaks}"


def test_latency_stays_within_the_ci_ceiling(pristine_corpus: Engine) -> None:
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        outcomes = list(
            pool.map(lambda index: run_worker(pristine_corpus, index), range(CONCURRENCY))
        )

    latencies = sorted(sample for batch, _ in outcomes for sample in batch)
    assert latencies
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] * 1000
    p50 = statistics.median(latencies) * 1000
    assert p95 <= CI_P95_CEILING_MS, (
        f"p95 {p95:.0f} ms (p50 {p50:.0f} ms) at {CONCURRENCY} concurrent — "
        "above the CI ceiling, which means an algorithmic regression, not a slow machine"
    )
