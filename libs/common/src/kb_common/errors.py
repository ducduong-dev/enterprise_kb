"""Error hierarchy shared by every service.

Rule: security-relevant failures (`AuthzError`, `PolicyViolation`, `GateBlocked`) must never
be swallowed into a generic 500 and must never leak the resolved filter or document existence
to the caller. `existence_disclosure` on a category decides whether "not found" and "not
allowed" are distinguishable — the default is to say "not found" (see INV-10).
"""

from __future__ import annotations

from typing import Any


class KBError(Exception):
    """Base for all platform errors."""

    status_code: int = 500
    code: str = "internal_error"
    # Whether the message is safe to return to an end user verbatim.
    public: bool = False

    def __init__(self, message: str, /, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def public_payload(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message if self.public else "Request could not be completed",
        }


class ConfigError(KBError):
    code = "config_error"


class NotFound(KBError):
    status_code = 404
    code = "not_found"
    public = True


class Conflict(KBError):
    status_code = 409
    code = "conflict"
    public = True


class ValidationError(KBError):
    status_code = 422
    code = "validation_error"
    public = True


class AuthenticationError(KBError):
    """No credential, expired token, bad signature, wrong issuer/audience."""

    status_code = 401
    code = "unauthenticated"
    public = True


class AuthzError(KBError):
    """Authenticated but not permitted. Surfaced as 404 for non-disclosing categories."""

    status_code = 403
    code = "forbidden"
    public = True


class PolicyViolation(AuthzError):
    """A request tried to widen a server-side filter (INV-2) or bypass a scope binding.

    This is an attack signal, not a user mistake: always audit-logged at WARN with the
    principal, the attempted widening, and the resolved base filter.
    """

    status_code = 403
    code = "policy_violation"
    public = True


class GateBlocked(KBError):
    """A fail-closed gate refused the operation (PII gate INV-7, class guard INV-8)."""

    status_code = 409
    code = "gate_blocked"
    public = True


class RetentionHold(KBError):
    """Deletion/purge refused because `retention_until` has not elapsed (INV-9)."""

    status_code = 409
    code = "retention_hold"
    public = True


class UpstreamError(KBError):
    """A port adapter (model gateway, index, storage) failed."""

    status_code = 502
    code = "upstream_error"


class RateLimited(KBError):
    """The caller has used its budget for the window.

    Public by design: a client that cannot tell "slow down" from "something broke" retries
    immediately and makes the problem worse.
    """

    status_code = 429
    code = "rate_limited"
    public = True
