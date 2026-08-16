"""ASGI entrypoint for kb-retrieval-api.

The only service permitted to read from an index (INV-1). Every other service — chat-api,
portal-api, the external bot — calls these endpoints, presenting the end user's token or its
own service token; none of them can widen what comes back, because the filter is built here
from the verified principal (INV-2).

Not exposed to end users directly: it sits behind portal-api and chat-api on the internal
network, and behind the DMZ profile for the external surface (M8).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header
from kb_authz.principal import Principal
from kb_authz.tokens import (
    OidcTokenVerifier,
    SharedSecretVerifier,
    TokenVerifier,
    resolve_principal,
)
from kb_common.app import create_app
from kb_common.audit import SqlAuditSink
from kb_common.config import get_settings
from kb_common.db import get_session
from kb_common.errors import AuthenticationError
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.embedding_tei import TeiEmbeddingAdapter
from kb_ports.adapters.pg_search_index import PgSearchIndexAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter, TeiRerankAdapter
from kb_ports.indexes import KeywordIndexPort, VectorIndexPort
from kb_ports.models import EmbeddingPort, RerankPort
from kb_schemas.api import (
    CitationLookupRequest,
    ResolveAnchorRequest,
    ResolveAnchorResponse,
    RetrieveRequest,
    RetrieveResponse,
)
from sqlalchemy.orm import Session

from kb_retrieval_api.engine import RetrievalEngine

app: FastAPI = create_app("kb-retrieval-api")


def _verifier() -> TokenVerifier:
    settings = get_settings()
    if settings.oidc.allow_insecure_tokens and not settings.is_production:
        return SharedSecretVerifier()
    return OidcTokenVerifier(settings.oidc)


def current_principal(authorization: str = Header(default="")) -> Principal:
    """The verified caller. Everything the filter is built from comes from this token —
    nothing from the request body."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("a bearer token is required")
    return resolve_principal(token, _verifier())


def keyword_index(session: Session = Depends(get_session)) -> KeywordIndexPort:
    """`KB_KEYWORD_BACKEND` selects; the caller only knows the port (ADR-0021)."""
    settings = get_settings()
    if settings.keyword.backend == "pg_search":
        return PgSearchIndexAdapter(session)
    return PostgresFtsIndexAdapter(session)


def vector_index(session: Session = Depends(get_session)) -> VectorIndexPort:
    return PgVectorIndexAdapter(session)


def embedder() -> EmbeddingPort:
    settings = get_settings()
    # The deterministic adapter is dev/CI only and says so in its `info`; production runs
    # BGE-M3 on the GPU node.
    return HashedEmbeddingAdapter() if settings.use_deterministic_models else TeiEmbeddingAdapter()


def reranker() -> RerankPort:
    settings = get_settings()
    return LexicalRerankAdapter() if settings.use_deterministic_models else TeiRerankAdapter()


def engine(
    session: Session = Depends(get_session),
    keyword: KeywordIndexPort = Depends(keyword_index),
    vector: VectorIndexPort = Depends(vector_index),
    embed: EmbeddingPort = Depends(embedder),
    rerank: RerankPort = Depends(reranker),
) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=keyword,
        vector_index=vector,
        embedder=embed,
        reranker=rerank,
        audit=SqlAuditSink(session),
    )


@app.post("/v1/retrieve", response_model=RetrieveResponse)
def retrieve(
    request: RetrieveRequest,
    principal: Principal = Depends(current_principal),
    retrieval: RetrievalEngine = Depends(engine),
) -> RetrieveResponse:
    return retrieval.retrieve(principal, request).response


@app.post("/v1/citation-lookup", response_model=RetrieveResponse)
def citation_lookup(
    request: CitationLookupRequest,
    principal: Principal = Depends(current_principal),
    retrieval: RetrievalEngine = Depends(engine),
) -> RetrieveResponse:
    return retrieval.citation_lookup(principal, request).response


@app.post("/v1/resolve-anchor", response_model=ResolveAnchorResponse)
def resolve_anchor(
    request: ResolveAnchorRequest,
    principal: Principal = Depends(current_principal),
    retrieval: RetrievalEngine = Depends(engine),
) -> ResolveAnchorResponse:
    """Resolve a reference the platform parsed itself to the clauses it names.

    Distinct from `/v1/citation-lookup`, which trigram-matches text a *human* typed against
    citation labels. Here the document is already identified and the anchor is already
    structured, so this is an equality join with nothing to tune — and it runs the full chunk
    predicate, so a reference into a document the caller may not read, or into a clause no
    longer in force, resolves to nothing (ADR-0036).
    """
    return retrieval.resolve_anchors(principal, request)[0]


@app.get("/v1/documents/{document_id}")
def get_document(
    document_id: uuid.UUID,
    version_id: uuid.UUID | None = None,
    principal: Principal = Depends(current_principal),
    retrieval: RetrievalEngine = Depends(engine),
) -> dict[str, Any]:
    """Render-time access re-check (gate 3). Archived-version reads are audited separately."""
    return retrieval.document_access(principal, document_id, version_id=version_id)


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
