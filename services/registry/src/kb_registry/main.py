"""ASGI entrypoint for kb-registry.

Registry owns documents, versions, categories, edges and (from M2) the publish transaction
(INV-5). It is an internal service: portal-api and the Temporal activities call it, end users
never do, and it serves no search path — reads for retrieval go through retrieval-api (INV-1).
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Header, Query
from kb_common.app import create_app
from kb_common.audit import SqlAuditSink
from kb_common.db import get_session
from kb_common.errors import NotFound
from kb_schemas.orm import CategoryRow
from pydantic import BaseModel
from sqlalchemy.orm import Session

from kb_registry import repository as repo
from kb_registry.schemas import (
    CategoryCreate,
    DetectedRefIn,
    DocumentCreate,
    DocumentOut,
    DocumentUpdate,
    RefLinkResult,
    ReviewTaskOut,
    VersionCreate,
    VersionOut,
)
from kb_registry.service import RegistryService

app: FastAPI = create_app("kb-registry")


def service(session: Session = Depends(get_session)) -> RegistryService:
    return RegistryService(session, audit=SqlAuditSink(session))


def actor(x_kb_actor: str = Header(default="system")) -> str:
    """Caller identity for the audit trail.

    Registry is reachable only from inside the platform, so the calling service asserts the
    acting user here. The ACL decisions that matter to end users are made in retrieval-api
    from a verified token (INV-2); this header exists so writes are attributable.
    """
    return x_kb_actor


class CategoryOut(BaseModel):
    path: str
    label: str
    default_visibility: str
    default_allowed_groups: list[str]
    steward_group: str | None
    existence_disclosure: bool


# ------------------------------------------------------------------------------ categories


@app.get("/v1/categories", response_model=list[CategoryOut])
def list_categories(
    prefix: str | None = None, session: Session = Depends(get_session)
) -> list[CategoryOut]:
    return [
        CategoryOut.model_validate(row, from_attributes=True)
        for row in repo.list_categories(session, prefix)
    ]


@app.post("/v1/categories", response_model=CategoryOut, status_code=201)
def create_category(spec: CategoryCreate, session: Session = Depends(get_session)) -> CategoryOut:
    row = repo.add_category(
        session,
        CategoryRow(
            path=spec.path,
            label=spec.label,
            default_visibility=spec.default_visibility.value,
            default_allowed_groups=spec.default_allowed_groups,
            steward_group=spec.steward_group,
            existence_disclosure=spec.existence_disclosure,
        ),
    )
    return CategoryOut.model_validate(row, from_attributes=True)


# ------------------------------------------------------------------------------- documents


@app.post("/v1/documents", response_model=DocumentOut, status_code=201)
def create_document(
    spec: DocumentCreate,
    registry: RegistryService = Depends(service),
    who: str = Depends(actor),
) -> DocumentOut:
    return DocumentOut.model_validate(registry.create_document(spec, actor=who))


@app.get("/v1/documents", response_model=list[DocumentOut])
def list_documents(
    category: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[DocumentOut]:
    rows = repo.list_documents(
        session, category_prefix=category, status=status, limit=limit, offset=offset
    )
    return [DocumentOut.model_validate(row) for row in rows]


@app.get("/v1/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: uuid.UUID, session: Session = Depends(get_session)) -> DocumentOut:
    row = repo.get_document(session, document_id)
    if row is None:
        raise NotFound("document not found", document_id=str(document_id))
    return DocumentOut.model_validate(row)


@app.patch("/v1/documents/{document_id}", response_model=DocumentOut)
def update_document(
    document_id: uuid.UUID,
    spec: DocumentUpdate,
    registry: RegistryService = Depends(service),
    who: str = Depends(actor),
) -> DocumentOut:
    return DocumentOut.model_validate(registry.update_document(document_id, spec, actor=who))


@app.get("/v1/documents/{document_id}/versions", response_model=list[VersionOut])
def list_versions(
    document_id: uuid.UUID, session: Session = Depends(get_session)
) -> list[VersionOut]:
    return [VersionOut.model_validate(row) for row in repo.list_versions(session, document_id)]


@app.post("/v1/documents/{document_id}/refs", response_model=RefLinkResult)
def link_refs(
    document_id: uuid.UUID,
    refs: list[DetectedRefIn],
    registry: RegistryService = Depends(service),
) -> RefLinkResult:
    created, unresolved = registry.link_detected_refs(document_id, refs)
    return RefLinkResult(created=created, unresolved=unresolved)


# -------------------------------------------------------------------------------- versions


@app.post("/v1/versions", response_model=VersionOut, status_code=201)
def create_version(
    spec: VersionCreate,
    registry: RegistryService = Depends(service),
    who: str = Depends(actor),
) -> VersionOut:
    return VersionOut.model_validate(registry.create_version(spec, actor=who))


@app.get("/v1/versions/{version_id}", response_model=VersionOut)
def get_version(version_id: uuid.UUID, session: Session = Depends(get_session)) -> VersionOut:
    row = repo.get_version(session, version_id)
    if row is None:
        raise NotFound("version not found", version_id=str(version_id))
    return VersionOut.model_validate(row)


# ---------------------------------------------------------------------------- review tasks


@app.get("/v1/review-tasks", response_model=list[ReviewTaskOut])
def list_review_tasks(
    groups: list[str] | None = Query(default=None),
    state: str = "open",
    task_type: str | None = None,
    session: Session = Depends(get_session),
) -> list[ReviewTaskOut]:
    rows = repo.list_review_tasks(session, assignee_groups=groups, state=state, task_type=task_type)
    return [ReviewTaskOut.model_validate(row) for row in rows]


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
