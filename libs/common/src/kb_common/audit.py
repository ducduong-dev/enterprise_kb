"""Audit trail (INV-11).

The audit log is a database table, not a log stream: it must survive log rotation, be
queryable by Compliance, and be written inside the same transaction as the action it
records where that action is transactional (publish, override, purge).

Every retrieval writes one record carrying principal, on-behalf-of user, the *resolved*
filter, and the chunk/version IDs returned, so any answer can be reconstructed later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_common.logging import get_logger
from kb_common.metrics import audit_fallback

log = get_logger(__name__)


class AuditAction:
    """Canonical action names. Add here, never inline a string at a call site."""

    RETRIEVE = "retrieve"
    CITATION_LOOKUP = "citation_lookup"
    DOCUMENT_RENDER = "document_render"
    ARCHIVED_VERSION_ACCESS = "archived_version_access"
    CHAT_ANSWER = "chat_answer"
    UPLOAD = "upload"
    DOCUMENT_CREATE = "document_create"
    VERSION_CREATE = "version_create"
    #: A version's metadata corrected before publication — today only its effective date, which
    #: decides what a point-in-time query returns, so the change is worth a name of its own.
    VERSION_UPDATE = "version_update"
    PUBLISH = "publish"
    PII_OVERRIDE = "pii_override"
    PII_BLOCK = "pii_block"
    REVIEW_DECISION = "review_decision"
    MERGE_APPROVAL = "merge_approval"
    POLICY_VIOLATION = "policy_violation"
    PURGE_ATTEMPT = "purge_attempt"
    ACL_CHANGE = "acl_change"


@dataclass(slots=True)
class AuditRecord:
    action: str
    actor: str
    on_behalf_of: str | None = None
    object_ref: dict[str, Any] = field(default_factory=dict)
    resolved_filter: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditSink(Protocol):
    def write(self, record: AuditRecord) -> None: ...


class SqlAuditSink:
    """Writes to `audit_log`.

    Pass the *caller's* session when the audited action is part of a transaction (publish,
    PII override) so the record commits or rolls back with it. Pass a dedicated session for
    read-path auditing so a slow audit write cannot hold a retrieval transaction open.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def write(self, record: AuditRecord) -> None:
        self._session.execute(
            text(
                """
                INSERT INTO audit_log (ts, actor, on_behalf_of, action, object_ref,
                                       resolved_filter, detail)
                VALUES (:ts, :actor, :on_behalf_of, :action,
                        CAST(:object_ref AS JSONB), CAST(:resolved_filter AS JSONB),
                        CAST(:detail AS JSONB))
                """
            ),
            {
                "ts": record.ts,
                "actor": record.actor,
                "on_behalf_of": record.on_behalf_of,
                "action": record.action,
                "object_ref": _json(record.object_ref),
                "resolved_filter": _json(record.resolved_filter),
                "detail": _json(record.detail),
            },
        )


class InMemoryAuditSink:
    """Tests and local dev. Assertions in the ACL sweep read `records`."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)

    def by_action(self, action: str) -> list[AuditRecord]:
        return [r for r in self.records if r.action == action]


class LoggingAuditSink:
    """Fallback only — used when the DB is unreachable so an audit event is never lost
    silently. Alerting treats any line from this sink as a P2."""

    def write(self, record: AuditRecord) -> None:
        audit_fallback.inc()
        log.warning(
            "audit_fallback",
            extra={
                "audit_action": record.action,
                "actor": record.actor,
                "on_behalf_of": record.on_behalf_of,
                "object_ref": record.object_ref,
            },
        )


def _json(value: Any) -> str | None:
    import json

    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)
