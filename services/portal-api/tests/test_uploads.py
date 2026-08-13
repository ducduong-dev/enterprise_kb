"""Upload API: authentication, validation, storage, workflow hand-off, audit."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from kb_common.config import get_settings, reset_settings_cache
from kb_common.db import get_session
from kb_common.errors import AuthzError
from kb_portal_api.auth import reset_verifier_cache
from kb_portal_api.main import app, retrieval, starter, storage
from kb_portal_api.workflows import InMemoryStarter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_schemas.api import CitationLookupRequest, RetrieveRequest, RetrieveResponse
from kb_schemas.orm import CategoryRow, DocumentRow, DocumentVersionRow
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_portal_docs"
SECRET = "kb-test-secret"


def make_token(
    subject: str = "u-retail-staff",
    groups: list[str] | None = None,
    roles: list[str] | None = None,
) -> str:
    settings = get_settings().oidc
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "iss": settings.issuer,
            "aud": settings.audience,
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "preferred_username": subject,
            "groups": groups if groups is not None else ["/dept/retail"],
            "realm_access": {"roles": roles or []},
            "azp": "kb-portal",
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(autouse=True)
def insecure_tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Dev/test token path. `get_settings()` refuses this combination outside dev/test."""
    monkeypatch.setenv("KB_ENV", "test")
    monkeypatch.setenv("KB_OIDC_ALLOW_INSECURE_TOKENS", "true")
    reset_settings_cache()
    reset_verifier_cache()
    yield
    reset_settings_cache()
    reset_verifier_cache()


@pytest.fixture
def workflows() -> InMemoryStarter:
    return InMemoryStarter()


@pytest.fixture
def client(session: Session, tmp_path: Path, workflows: InMemoryStarter) -> Iterator[TestClient]:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="Portal test",
                default_visibility="internal_all",
                default_allowed_groups=[],
                steward_group="dept/operations",
                existence_disclosure=False,
            ),
        )
        session.commit()

    app.dependency_overrides[storage] = lambda: LocalStorageAdapter(tmp_path)
    app.dependency_overrides[starter] = lambda: workflows
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def upload(client: TestClient, *, token: str | None = None, **fields: str) -> Response:
    data = {"category_path": CATEGORY, "doc_class": "operational", **fields}
    headers = {"Authorization": f"Bearer {token or make_token()}"}
    response: Response = client.post(
        "/v1/uploads",
        files={"file": ("notice.txt", b"THONG BAO\n\nNoi dung.\n", "text/plain")},
        data=data,
        headers=headers,
    )
    return response


# ------------------------------------------------------------------------- authentication


def test_anonymous_uploads_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/uploads",
        files={"file": ("x.txt", b"hello", "text/plain")},
        data={"category_path": CATEGORY},
    )
    assert response.status_code == 401


def test_a_forged_token_is_rejected(client: TestClient) -> None:
    forged = jwt.encode({"sub": "attacker"}, "wrong-secret", algorithm="HS256")
    assert upload(client, token=forged).status_code == 401


# ------------------------------------------------------------------------------ validation


def test_an_unknown_category_is_refused_before_anything_is_stored(
    client: TestClient, tmp_path: Path
) -> None:
    response = upload(client, category_path="t_does_not_exist")
    assert response.status_code == 422
    assert not any(tmp_path.rglob("*"))


def test_an_unknown_doc_class_is_refused(client: TestClient) -> None:
    assert upload(client, doc_class="something_else").status_code == 422


def test_an_empty_file_is_refused(client: TestClient) -> None:
    response = client.post(
        "/v1/uploads",
        files={"file": ("empty.txt", b"", "text/plain")},
        data={"category_path": CATEGORY, "doc_class": "operational"},
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------- happy path


def test_upload_stores_the_original_and_starts_the_workflow(
    client: TestClient, workflows: InMemoryStarter, tmp_path: Path
) -> None:
    response = upload(client, title="Thông báo phí")
    assert response.status_code == 202
    body = response.json()
    assert body["workflow_id"].startswith("ingest-")
    assert len(body["document_hash"]) == 64

    # The bytes are durable before the workflow is asked to do anything.
    stored_files = list(tmp_path.rglob("*"))
    assert any(path.is_file() for path in stored_files)

    (workflow_id, request), *rest = workflows.started
    assert not rest
    assert workflow_id == body["workflow_id"]
    # The workflow receives a reference, never the document bytes (ADR-0005).
    assert request.upload.content_hash == body["document_hash"]
    assert request.classification.category_path == CATEGORY
    assert request.classification.title == "Thông báo phí"
    assert request.actor == "u-retail-staff"


def test_re_uploading_the_same_bytes_is_reported_not_duplicated(
    client: TestClient, tmp_path: Path
) -> None:
    first = upload(client).json()
    second = upload(client).json()
    assert first["document_hash"] == second["document_hash"]
    assert first["already_stored"] is False
    assert second["already_stored"] is True
    objects = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(objects) == 1


def test_the_upload_is_audited(client: TestClient, session: Session) -> None:
    body = upload(client).json()
    row = (
        session.execute(
            text(
                "SELECT actor, action, object_ref FROM audit_log "
                "WHERE object_ref->>'upload_id' = :upload_id"
            ),
            {"upload_id": body["upload_id"]},
        )
        .mappings()
        .one()
    )
    assert row["actor"] == "u-retail-staff"
    assert row["action"] == "upload"


def test_upload_status_is_reported_from_the_workflow(
    client: TestClient, workflows: InMemoryStarter
) -> None:
    body = upload(client).json()
    workflows.statuses[body["workflow_id"]] = "awaiting_review"
    workflows.results[body["workflow_id"]] = {"status": "awaiting_review", "document_id": "d1"}

    response = client.get(
        f"/v1/uploads/{body['workflow_id']}",
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_review"
    assert response.json()["outcome"]["document_id"] == "d1"


def test_unknown_workflow_is_a_404(client: TestClient) -> None:
    response = client.get(
        "/v1/uploads/ingest-does-not-exist",
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert response.status_code == 404


# ----------------------------------------------------------------------------- queues


def test_reviewers_only_see_their_own_queues(client: TestClient, session: Session) -> None:
    """A task queue is visible to the group that owns it, and to nobody else."""
    document = repo.add_document(
        session,
        _document_row(),
    )
    version = repo.add_version(session, _version_row(document.id))
    from kb_registry.service import RegistryService
    from kb_schemas.enums import ReviewTaskType

    RegistryService(session).open_review_task(
        version.id, ReviewTaskType.IDP_REVIEW, assignee_group="dept/legal"
    )
    session.commit()

    legal = client.get(
        "/v1/review-tasks",
        headers={"Authorization": f"Bearer {make_token(groups=['/dept/legal'])}"},
    )
    retail = client.get(
        "/v1/review-tasks",
        headers={"Authorization": f"Bearer {make_token(groups=['/dept/retail'])}"},
    )
    assert any(task["assignee_group"] == "dept/legal" for task in legal.json())
    assert all(task["assignee_group"] != "dept/legal" for task in retail.json())


def _document_row() -> DocumentRow:
    return DocumentRow(
        id=uuid.uuid4(),
        title="Queue fixture",
        legal_number=None,
        doc_class="operational",
        category_path=CATEGORY,
        department=None,
        visibility="internal_all",
        allowed_groups=[],
        status="draft",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _version_row(document_id: uuid.UUID) -> DocumentVersionRow:
    return DocumentVersionRow(
        id=uuid.uuid4(),
        document_id=document_id,
        content_ref="kb-originals/x",
        content_hash=uuid.uuid4().hex,
        source_type="upload",
        author="tester",
        pii_status="pending",
        is_canonical=False,
        created_at=datetime.now(UTC),
    )


def test_categories_drive_the_upload_form(client: TestClient) -> None:
    response = client.get("/v1/categories", headers={"Authorization": f"Bearer {make_token()}"})
    assert response.status_code == 200
    assert any(row["path"] == CATEGORY for row in response.json())


def test_categories_require_authentication(client: TestClient) -> None:
    assert client.get("/v1/categories").status_code == 401


# ------------------------------------------------------------------------------- search


@dataclass
class RecordingRetrievalClient:
    """Stands in for retrieval-api, and records what portal-api forwarded."""

    calls: list[tuple[str, RetrieveRequest | CitationLookupRequest]] = dc_field(
        default_factory=list
    )

    def retrieve(self, token: str, request: RetrieveRequest) -> RetrieveResponse:
        self.calls.append((token, request))
        return RetrieveResponse(chunks=[], resolved_filter_id="filter-1")

    def citation_lookup(self, token: str, request: CitationLookupRequest) -> RetrieveResponse:
        self.calls.append((token, request))
        return RetrieveResponse(chunks=[], resolved_filter_id="filter-2")


@pytest.fixture
def retrieval_client(client: TestClient) -> RecordingRetrievalClient:
    recorder = RecordingRetrievalClient()
    app.dependency_overrides[retrieval] = lambda: recorder
    return recorder


def test_search_forwards_the_users_own_token(
    client: TestClient, retrieval_client: RecordingRetrievalClient
) -> None:
    """The filter downstream must be built from the end user, not from portal-api (INV-2)."""
    token = make_token()
    response = client.post(
        "/v1/search",
        json={"query": "tỷ lệ an toàn vốn", "top_k": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["resolved_filter_id"] == "filter-1"

    ((forwarded_token, forwarded),) = retrieval_client.calls
    assert forwarded_token == token
    assert isinstance(forwarded, RetrieveRequest)
    assert forwarded.query == "tỷ lệ an toàn vốn"
    assert forwarded.top_k == 5


def test_search_passes_facets_through_as_narrowing(
    client: TestClient, retrieval_client: RecordingRetrievalClient
) -> None:
    client.post(
        "/v1/search",
        json={"query": "vốn", "category": "regulations.sbv", "department": "risk"},
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    ((_, forwarded),) = retrieval_client.calls
    assert isinstance(forwarded, RetrieveRequest)
    assert forwarded.facets is not None
    assert forwarded.facets.category == "regulations.sbv"
    assert forwarded.facets.department == "risk"


def test_search_rejects_unknown_fields(client: TestClient) -> None:
    """There must be no request field capable of widening the filter."""
    response = client.post(
        "/v1/search",
        json={"query": "vốn", "visibility": "restricted"},
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert response.status_code == 422


def test_search_requires_authentication(client: TestClient) -> None:
    assert client.post("/v1/search", json={"query": "vốn"}).status_code == 401


def test_a_refusal_from_retrieval_is_passed_through(client: TestClient) -> None:
    """portal-api must not soften the funnel's decision, nor turn it into empty results."""

    class RefusingClient:
        def retrieve(self, token: str, request: RetrieveRequest) -> RetrieveResponse:
            raise AuthzError("retrieval refused the request", status=403)

        def citation_lookup(self, token: str, request: CitationLookupRequest) -> RetrieveResponse:
            raise AuthzError("retrieval refused the request", status=403)

    app.dependency_overrides[retrieval] = RefusingClient
    response = client.post(
        "/v1/search",
        json={"query": "vốn"},
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert response.status_code == 403


# ------------------------------------------------------------------------- review editor


def test_the_review_endpoints_require_authentication(client: TestClient) -> None:
    task_id = uuid.uuid4()
    assert client.get(f"/v1/review-tasks/{task_id}").status_code == 401
    assert client.get(f"/v1/review-tasks/{task_id}/pages/1").status_code == 401
    assert (
        client.post(
            f"/v1/review-tasks/{task_id}/decision", json={"decision": "approve"}
        ).status_code
        == 401
    )


def test_an_unknown_review_task_is_not_found(client: TestClient) -> None:
    response = client.get(
        f"/v1/review-tasks/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {make_token(groups=['/dept/legal'])}"},
    )
    assert response.status_code == 404


def test_a_reviewer_with_no_group_cannot_review(client: TestClient) -> None:
    """Review authority comes from group membership, not from being signed in."""
    response = client.get(
        f"/v1/review-tasks/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {make_token(groups=[])}"},
    )
    assert response.status_code == 403


def test_a_decision_must_be_approve_or_reject(client: TestClient) -> None:
    response = client.post(
        f"/v1/review-tasks/{uuid.uuid4()}/decision",
        json={"decision": "maybe"},
        headers={"Authorization": f"Bearer {make_token(groups=['/dept/legal'])}"},
    )
    # Unknown task and invalid decision both refuse; neither reveals the other.
    assert response.status_code in (404, 422)


def test_the_decision_body_rejects_unknown_fields(client: TestClient) -> None:
    response = client.post(
        f"/v1/review-tasks/{uuid.uuid4()}/decision",
        json={"decision": "approve", "publish_without_review": True},
        headers={"Authorization": f"Bearer {make_token(groups=['/dept/legal'])}"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- pii override


def test_the_pii_override_needs_the_role(client: TestClient) -> None:
    """INV-7: overriding a PII block is not something any signed-in user may do."""
    response = client.post(
        f"/v1/versions/{uuid.uuid4()}/pii-override",
        json={"justification": "Tài khoản nội bộ của ngân hàng, đã đối chiếu với Vận hành."},
        headers={"Authorization": f"Bearer {make_token(groups=['/dept/retail'])}"},
    )
    assert response.status_code == 403


def test_the_pii_override_requires_authentication(client: TestClient) -> None:
    response = client.post(
        f"/v1/versions/{uuid.uuid4()}/pii-override", json={"justification": "x" * 40}
    )
    assert response.status_code == 401


def test_the_override_body_rejects_unknown_fields(client: TestClient) -> None:
    """No request field may stand in for the justification."""
    response = client.post(
        f"/v1/versions/{uuid.uuid4()}/pii-override",
        json={"justification": "x" * 40, "skip_audit": True},
        headers={"Authorization": f"Bearer {make_token(roles=['kb-pii-overrider'])}"},
    )
    assert response.status_code == 422


def test_a_workflow_result_is_json_whichever_shape_temporal_returns() -> None:
    """Temporal decodes the result as a plain dict when the caller declares no result type;
    only an in-process call ever sees the dataclass. The upload screen 500'd on the dict."""
    from dataclasses import dataclass as _dataclass

    from kb_portal_api.workflows import _as_dict

    @_dataclass
    class Outcome:
        status: str
        document_id: str

    assert _as_dict({"status": "awaiting_review", "document_id": "d1"}) == {
        "status": "awaiting_review",
        "document_id": "d1",
    }
    assert _as_dict(Outcome(status="awaiting_review", document_id="d1")) == {
        "status": "awaiting_review",
        "document_id": "d1",
    }
