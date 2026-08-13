"""Client for retrieval-api — the single funnel (INV-1).

Every surface that shows a user document text goes through here: the portal's search screen
and both chat surfaces. None of them queries an index, and none of them holds an ACL decision.

The client takes a raw bearer token rather than a `Principal` on purpose. A caller has no
authority to assert *who* is asking — only to pass on the token it was given, so the filter
downstream is built from an identity retrieval-api verified itself (INV-2). A refusal comes
back as a refusal: a client that turned 403 into an empty result set would let a caller show
"no documents found" for material the user simply may not see, which is a different sentence
with different consequences.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from kb_common.config import get_settings
from kb_common.errors import AuthzError, UpstreamError
from kb_common.logging import get_logger
from kb_schemas.api import CitationLookupRequest, RetrieveRequest, RetrieveResponse

log = get_logger(__name__)

#: Retrieval is user-facing latency: fail fast rather than hold a page open.
TIMEOUT_SECONDS = 15.0


class RetrievalClient(Protocol):
    def retrieve(self, token: str, request: RetrieveRequest) -> RetrieveResponse: ...

    def citation_lookup(self, token: str, request: CitationLookupRequest) -> RetrieveResponse: ...


class HttpRetrievalClient:
    def __init__(self, base_url: str | None = None, timeout: float = TIMEOUT_SECONDS) -> None:
        settings = get_settings()
        # In compose this is the service name on the platform network; the DMZ profile points
        # its own instance at its own funnel. Still one funnel per deployment, and no caller
        # may choose a different one per request (INV-1).
        self._base_url = base_url or settings.retrieval_url
        self._timeout = timeout
        self._service = settings.service_name

    def _post(self, path: str, token: str, payload: dict[str, Any]) -> RetrieveResponse:
        try:
            response = httpx.post(
                f"{self._base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("retrieval service unreachable", path=path) from exc

        if response.status_code in (401, 403):
            # Pass the refusal through unchanged: a caller must not soften a decision the
            # funnel made, and must not turn it into an empty result set either.
            raise AuthzError("retrieval refused the request", status=response.status_code)
        if response.status_code >= 400:
            raise UpstreamError("retrieval failed", status=response.status_code, path=path)
        return RetrieveResponse.model_validate(response.json())

    def retrieve(self, token: str, request: RetrieveRequest) -> RetrieveResponse:
        return self._post("/v1/retrieve", token, request.model_dump(mode="json"))

    def citation_lookup(self, token: str, request: CitationLookupRequest) -> RetrieveResponse:
        return self._post("/v1/citation-lookup", token, request.model_dump(mode="json"))
