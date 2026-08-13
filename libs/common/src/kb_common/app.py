"""FastAPI application factory — identical middleware/error handling in every service."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from kb_common.config import Settings, get_settings
from kb_common.errors import KBError, PolicyViolation
from kb_common.logging import (
    configure_logging,
    get_logger,
    log_context,
    new_request_id,
    safe_extra,
)
from kb_common.metrics import CONTENT_TYPE, policy_violations, render

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


def create_app(service_name: str, settings: Settings | None = None, **kwargs: object) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(service_name, cfg.log_level, cfg.log_format)

    app = FastAPI(title=service_name, version="0.1.0", **kwargs)  # type: ignore[arg-type]
    app.state.settings = cfg
    app.state.service_name = service_name

    if cfg.cors_origins:
        # Named origins only — never `*`, and never with credentials. The portal authenticates
        # with a bearer token it holds in session storage, so the browser must be allowed to
        # send `Authorization`; it sends no cookie, and `allow_credentials=True` alongside a
        # wildcard is the combination that turns a CSRF into a data read.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
    ) -> JSONResponse:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        started = time.perf_counter()
        with log_context(request_id=request_id, path=request.url.path, method=request.method):
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            log.info(
                "request_complete",
                extra={
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return response

    @app.exception_handler(PolicyViolation)
    async def policy_violation_handler(_: Request, exc: PolicyViolation) -> JSONResponse:
        # Attack signal, not user error: always WARN with the attempted widening, and count
        # it — the alert rule fires on any non-zero rate.
        policy_violations.inc()
        log.warning(
            "policy_violation", extra=safe_extra({"detail": exc.detail, "reason": exc.message})
        )
        return JSONResponse(status_code=exc.status_code, content=exc.public_payload())

    @app.exception_handler(KBError)
    async def kb_error_handler(_: Request, exc: KBError) -> JSONResponse:
        level = log.warning if exc.status_code < 500 else log.error
        level(exc.code, extra=safe_extra({"detail": exc.detail, "reason": exc.message}))
        return JSONResponse(status_code=exc.status_code, content=exc.public_payload())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=render(), media_type=CONTENT_TYPE)

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        # Services override this with real dependency checks as they gain dependencies.
        return {"status": "ready", "service": service_name}

    return app
