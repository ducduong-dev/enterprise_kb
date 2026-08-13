"""Config guards, log redaction discipline, and the audit sink."""

from __future__ import annotations

import json
import logging

import pytest
from kb_common.audit import AuditAction, AuditRecord, InMemoryAuditSink
from kb_common.config import Settings, get_settings, reset_settings_cache
from kb_common.errors import KBError, NotFound, PolicyViolation
from kb_common.logging import JsonFormatter, log_context


def test_insecure_tokens_cannot_be_enabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_ENV", "prod")
    monkeypatch.setenv("KB_OIDC_ALLOW_INSECURE_TOKENS", "true")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="INSECURE"):
        get_settings()
    reset_settings_cache()


def test_database_password_is_not_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_DB_PASSWORD", "s3cret-not-in-logs")
    settings = Settings()
    assert settings.db.password.get_secret_value() == "s3cret-not-in-logs"
    # Settings objects end up in debug logs and exception context; the secret must not.
    assert "s3cret-not-in-logs" not in repr(settings.db)
    assert "s3cret-not-in-logs" not in repr(settings)


def test_json_log_lines_carry_bound_context() -> None:
    formatter = JsonFormatter("kb-test")
    record = logging.LogRecord("kb", logging.INFO, __file__, 1, "retrieve_complete", None, None)
    record.chunk_count = 3
    with log_context(request_id="r1", principal="u-1", on_behalf_of=None):
        payload = json.loads(formatter.format(record))
    assert payload["request_id"] == "r1"
    assert payload["principal"] == "u-1"
    assert payload["chunk_count"] == 3
    assert payload["service"] == "kb-test"


def test_log_context_is_scoped() -> None:
    formatter = JsonFormatter("kb-test")
    record = logging.LogRecord("kb", logging.INFO, __file__, 1, "x", None, None)
    with log_context(request_id="r1"):
        pass
    assert "request_id" not in json.loads(formatter.format(record))


def test_error_payloads_do_not_leak_internal_detail() -> None:
    """A 500 must not tell the caller what broke; a 404 may."""
    internal = KBError("connection to the index refused", host="kb-postgres:5432")
    assert "kb-postgres" not in json.dumps(internal.public_payload())

    visible = NotFound("document not found")
    assert visible.public_payload()["message"] == "document not found"


def test_policy_violation_carries_the_attempted_widening() -> None:
    violation = PolicyViolation("bad facet", requested="hr", permitted=["retail"])
    assert violation.status_code == 403
    assert violation.detail["requested"] == "hr"


def test_audit_sink_records_actor_and_delegate() -> None:
    sink = InMemoryAuditSink()
    sink.write(
        AuditRecord(
            action=AuditAction.RETRIEVE,
            actor="svc-internal-bot",
            on_behalf_of="u-retail-staff",
            object_ref={"chunk_ids": ["c1"]},
            resolved_filter={"filter_id": "abc"},
        )
    )
    (record,) = sink.by_action(AuditAction.RETRIEVE)
    assert record.actor == "svc-internal-bot"
    assert record.on_behalf_of == "u-retail-staff"
    assert record.resolved_filter == {"filter_id": "abc"}
    assert record.ts is not None


def test_reserved_extra_keys_cannot_crash_a_log_call() -> None:
    """`filename` and `message` are natural things to log about a document upload."""
    from kb_common.logging import get_logger, safe_extra

    logger = get_logger("kb.test.reserved")
    logger.info("upload", extra={"filename": "tt41.docx", "message": "x", "size": 12})

    assert safe_extra({"filename": "a", "size": 1}) == {"field_filename": "a", "size": 1}


# ------------------------------------------------------------------------------------ CORS
#
# The portal is served from its own origin and calls portal-api on another. Without the
# preflight answered, the browser blocks every request and the screens come up *empty* — no
# error, no spinner, just nothing, which reads as missing data rather than a blocked call.


def _app(origins: list[str]):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from kb_common.app import create_app

    app = create_app("kb-test", Settings(cors_origins=origins))

    @app.get("/v1/thing")
    def thing() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_a_configured_origin_may_preflight_an_authorized_call() -> None:
    response = _app(["http://localhost:5173"]).options(
        "/v1/thing",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_an_unlisted_origin_gets_no_grant() -> None:
    response = _app(["http://localhost:5173"]).get(
        "/v1/thing", headers={"Origin": "http://evil.example"}
    )
    assert "access-control-allow-origin" not in response.headers


def test_a_service_no_browser_calls_answers_no_preflight() -> None:
    """Default is empty: retrieval-api and the workers are called service-to-service."""
    response = _app([]).options(
        "/v1/thing",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 405
