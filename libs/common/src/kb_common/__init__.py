"""Shared platform utilities: config, logging, errors, audit, database."""

from kb_common.audit import AuditAction, AuditRecord, AuditSink, InMemoryAuditSink, SqlAuditSink
from kb_common.config import Settings, get_settings
from kb_common.errors import (
    AuthenticationError,
    AuthzError,
    ConfigError,
    Conflict,
    GateBlocked,
    KBError,
    NotFound,
    PolicyViolation,
    RetentionHold,
    UpstreamError,
    ValidationError,
)
from kb_common.logging import configure_logging, get_logger, log_context

__all__ = [
    "AuditAction",
    "AuditRecord",
    "AuditSink",
    "AuthenticationError",
    "AuthzError",
    "ConfigError",
    "Conflict",
    "GateBlocked",
    "InMemoryAuditSink",
    "KBError",
    "NotFound",
    "PolicyViolation",
    "RetentionHold",
    "Settings",
    "SqlAuditSink",
    "UpstreamError",
    "ValidationError",
    "configure_logging",
    "get_logger",
    "get_settings",
    "log_context",
]
