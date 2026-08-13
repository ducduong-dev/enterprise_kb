"""Structured JSON logging with request-scoped correlation context.

Logs are shipped to Loki. They are *operational* telemetry — the compliance record is the
`audit_log` table (see audit.py), never these lines. Nothing here may emit document text,
chunk text, or PII; log identifiers instead.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Default is None rather than {}: a mutable default would be shared across every context.
_context: ContextVar[dict[str, Any] | None] = ContextVar("kb_log_context", default=None)


def _fields() -> dict[str, Any]:
    return _context.get() or {}


_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)  # fmt: skip


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_fields())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__("%(levelname)-5s %(name)s: %(message)s")
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            **_fields(),
            **{
                k: v
                for k, v in record.__dict__.items()
                if k not in _RESERVED and not k.startswith("_")
            },
        }
        return f"{base} {extras}" if extras else base


def configure_logging(
    service_name: str = "kb-service", level: str = "INFO", fmt: str = "json"
) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(service_name) if fmt == "json" else ConsoleFormatter(service_name)
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn duplicates access logs in its own format; route them through ours.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class KBLogger(logging.Logger):
    """Logger that cannot be crashed by a colliding `extra` key.

    `logging` raises if an extra key shadows a LogRecord attribute — `filename`, `message`,
    `module`, `args`. Those are exactly the words a document platform wants to log, and a
    log line must never be able to fail the request it is describing. Colliding keys are
    prefixed with `field_` instead.
    """

    def makeRecord(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Any = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        return super().makeRecord(
            name,
            level,
            fn,
            lno,
            msg,
            args,
            exc_info,
            func,
            safe_extra(dict(extra)) if extra else extra,
            sinfo,
        )


logging.setLoggerClass(KBLogger)


def safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Make a dict safe to pass as logging `extra`.

    `logging` raises if an extra key collides with a LogRecord attribute — `message`, `args`,
    `module` and friends. A log call is the last place that should be able to take a request
    down, so reserved names are prefixed rather than rejected.
    """
    return {(f"field_{k}" if k in _RESERVED else k): v for k, v in fields.items()}


def new_request_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields (request_id, principal, on_behalf_of, workflow_id) to every log line."""
    token = _context.set({**_fields(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> MutableMapping[str, Any]:
    return dict(_fields())
