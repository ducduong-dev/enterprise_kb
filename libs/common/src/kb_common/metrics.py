"""Prometheus metrics.

Only metrics something acts on: each one below is either an alert in
`ops/prometheus/alerts.yml` or a number in an acceptance criterion. A dashboard full of
metrics nobody alerts on is a dashboard nobody reads.

Cardinality rule: no label may carry a document id, a user id or a query. Those belong in the
audit log, which is queryable and access-controlled; a metric label is neither.
"""

from __future__ import annotations

from prometheus_client import REGISTRY as DEFAULT_REGISTRY
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: INV-5: publish must be searchable within 10 s. Buckets straddle that budget so the alert
#: rule can read p95 against it directly.
publish_to_searchable = Histogram(
    "kb_publish_to_searchable_seconds",
    "Seconds from the publish commit to the chunk being searchable in the keyword index",
    buckets=(0.5, 1, 2, 5, 8, 10, 15, 30, 60, 300),
)

#: Backlog. A growing value means the corpus is serving stale canonical text.
outbox_unprocessed = Gauge("kb_outbox_unprocessed", "Outbox events awaiting the indexer")

outbox_processed = Counter(
    "kb_outbox_processed_total", "Outbox events consumed", labelnames=("topic", "outcome")
)

#: Every one of these is an attempt to widen a server-side filter (INV-2). Any non-zero rate
#: is investigated — this is an attack signal, not a usage statistic.
policy_violations = Counter(
    "kb_policy_violations_total", "Rejected attempts to widen the retrieval filter"
)

#: An audit record that could not reach the database (INV-11). Paged on.
audit_fallback = Counter(
    "kb_audit_fallback_total", "Audit records written to logs because the database was unreachable"
)

retrieval_latency = Histogram(
    "kb_retrieval_seconds",
    "End-to-end retrieval latency inside retrieval-api",
    labelnames=("stage",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

retrieval_results = Histogram(
    "kb_retrieval_results",
    "Chunks returned per retrieval, after the ACL filter and reranking",
    buckets=(0, 1, 2, 5, 10, 20, 50),
)


def render(registry: CollectorRegistry | None = None) -> bytes:
    return generate_latest(registry or DEFAULT_REGISTRY)


__all__ = [
    "CONTENT_TYPE",
    "audit_fallback",
    "outbox_processed",
    "outbox_unprocessed",
    "policy_violations",
    "publish_to_searchable",
    "render",
    "retrieval_latency",
    "retrieval_results",
]
