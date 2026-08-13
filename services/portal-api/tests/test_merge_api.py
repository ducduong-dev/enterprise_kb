"""The merge screen and its approvals over HTTP.

What is being tested is not "the endpoint returns 200". It is that the screen gives a reviewer
the three panes aligned section by section, and that the approval endpoint is the place where
four-eyes actually happens — a self-approval, a second click by the same person, or an approval
for a redrafted text is refused with a reason, over the wire, before any workflow is told.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from kb_common.config import get_settings, reset_settings_cache
from kb_common.db import get_session
from kb_idp.builder import KBDocBuilder
from kb_portal_api.auth import reset_verifier_cache
from kb_portal_api.main import app, starter, storage
from kb_portal_api.workflows import InMemoryStarter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_schemas.enums import ReviewTaskType
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import CategoryRow, DocumentRow, DocumentVersionRow, ReviewTaskRow
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_merge_api"
LEGAL_GROUP = "dept/legal"
SECRET = "kb-test-secret"
PREPARER = "u-merge-preparer"
BUCKET = "kb-derived"
WORKFLOW_ID = "merge-wf-1"

OLD = [("Điều 6. Tỷ lệ", "Tổ chức tín dụng duy trì tỷ lệ tối thiểu là 3% trên tổng số dư.")]
NEW = [
    ("Điều 6. Tỷ lệ", "Tổ chức tín dụng duy trì tỷ lệ tối thiểu là 5% trên tổng số dư."),
    ("Điều 7. Hiệu lực", "Điều này có hiệu lực từ ngày 01 tháng 01 năm 2027."),
]


def make_token(subject: str, groups: list[str] | None = None) -> str:
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
            "groups": groups if groups is not None else [f"/{LEGAL_GROUP}"],
            "realm_access": {"roles": []},
            "azp": "kb-portal",
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(autouse=True)
def insecure_tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KB_ENV", "test")
    monkeypatch.setenv("KB_OIDC_ALLOW_INSECURE_TOKENS", "true")
    reset_settings_cache()
    reset_verifier_cache()
    yield
    reset_settings_cache()
    reset_verifier_cache()


@pytest.fixture
def workflows() -> InMemoryStarter:
    started = InMemoryStarter()
    started.statuses[WORKFLOW_ID] = "awaiting_approval"
    return started


def kbdoc(sections: list[tuple[str, str]]) -> KBDoc:
    builder = KBDocBuilder(source_format="docx")
    for heading, body in sections:
        builder.add(heading, block_type="heading")
        builder.add(body)
    return builder.build(page_count=1)


class Merge:
    """A document with a canonical version and an incoming one, plus its review task."""

    def __init__(self, session: Session, store: LocalStorageAdapter) -> None:
        self.session = session
        self.storage = store

    def store_json(self, payload: bytes) -> str:
        stored = self.storage.put(BUCKET, io.BytesIO(payload), suffix=".json")
        return f"{BUCKET}/{stored.key}"

    def version(self, document_id: uuid.UUID, doc: KBDoc, *, canonical: bool) -> uuid.UUID:
        row = DocumentVersionRow(
            id=uuid.uuid4(),
            document_id=document_id,
            content_ref="kb-originals/x",
            content_hash=uuid.uuid4().hex,
            source_type="upload",
            author=PREPARER,
            idp_report_ref=self.store_json(doc.model_dump_json().encode("utf-8")),
            pii_status="clear",
            is_canonical=canonical,
            retention_until=datetime.now(UTC).date(),
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def build(self, *, doc_class: str = "regulatory", with_draft: bool = True) -> Merge:
        if repo.get_category(self.session, CATEGORY) is None:
            repo.add_category(
                self.session,
                CategoryRow(
                    path=CATEGORY,
                    label="Merge API tests",
                    default_visibility="internal_all",
                    default_allowed_groups=[],
                    steward_group=LEGAL_GROUP,
                    existence_disclosure=False,
                ),
            )
        document = DocumentRow(
            id=uuid.uuid4(),
            title="Thông tư về tỷ lệ",
            legal_number=f"{uuid.uuid4().hex[:6]}/2026/TT-MERGE",
            doc_class=doc_class,
            category_path=CATEGORY,
            visibility="internal_all",
            allowed_groups=[],
            status="published",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.session.add(document)
        self.session.flush()
        self.document_id = document.id

        self.canonical_id = self.version(document.id, kbdoc(OLD), canonical=True)
        document.canonical_version_id = self.canonical_id
        self.new_version_id = self.version(document.id, kbdoc(NEW), canonical=False)

        self.draft_ref = ""
        if with_draft:
            self.draft_ref = self.store_json(
                json.dumps(
                    {
                        "diff": {"touched_articles": [6, 7]},
                        "draft": {
                            "complete": True,
                            "model": "qwen-test",
                            "prompt_version": "1",
                            "substantive_changes": 2,
                            "classifications": [
                                {
                                    "section_path": "Điều 6",
                                    "bucket": "amended",
                                    "impact": "Tỷ lệ tăng từ 3% lên 5%.",
                                    "confidence": 0.9,
                                    "inferred": False,
                                },
                                {
                                    "section_path": "Điều 7",
                                    "bucket": "new_or_abrogated",
                                    "impact": "Điều mới.",
                                    "confidence": 0.9,
                                    "inferred": False,
                                },
                            ],
                            "sections": [
                                {
                                    "section_path": "Điều 6",
                                    "consolidated_text": "Điều 6. Tỷ lệ\nTỷ lệ tối thiểu là 5%.",
                                    "note": "Áp dụng sửa đổi.",
                                    "drafted": True,
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        task = ReviewTaskRow(
            id=uuid.uuid4(),
            version_id=self.new_version_id,
            task_type=ReviewTaskType.MERGE_REVIEW.value,
            state="open",
            assignee_group=LEGAL_GROUP,
            payload={
                "draft_ref": self.draft_ref,
                "document_id": str(document.id),
                "consolidation": True,
                "doc_class": doc_class,
                "prepared_by": PREPARER,
                "touched_articles": [6, 7],
                "workflow_id": WORKFLOW_ID,
            },
            created_at=datetime.now(UTC),
        )
        self.session.add(task)
        self.session.commit()
        self.task_id = task.id
        return self


@pytest.fixture
def store(tmp_path: Path) -> LocalStorageAdapter:
    return LocalStorageAdapter(tmp_path)


@pytest.fixture
def client(
    session: Session, store: LocalStorageAdapter, workflows: InMemoryStarter
) -> Iterator[TestClient]:
    app.dependency_overrides[storage] = lambda: store
    app.dependency_overrides[starter] = lambda: workflows
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def merge(session: Session, store: LocalStorageAdapter) -> Merge:
    return Merge(session, store).build()


def screen(client: TestClient, task_id: uuid.UUID, *, user: str = "u-legal-one") -> Response:
    response: Response = client.get(
        f"/v1/merge-tasks/{task_id}", headers={"Authorization": f"Bearer {make_token(user)}"}
    )
    return response


def decide(
    client: TestClient,
    task_id: uuid.UUID,
    *,
    user: str,
    decision: str = "approve",
    note: str = "",
    draft_ref: str = "",
) -> Response:
    response: Response = client.post(
        f"/v1/merge-tasks/{task_id}/decision",
        json={"decision": decision, "note": note, "draft_ref": draft_ref},
        headers={"Authorization": f"Bearer {make_token(user)}"},
    )
    return response


# ----------------------------------------------------------------------------- the screen


def test_the_screen_returns_three_aligned_panes(client: TestClient, merge: Merge) -> None:
    response = screen(client, merge.task_id)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()

    assert body["current_canonical"]["version_id"] == str(merge.canonical_id)
    assert body["new_version"]["version_id"] == str(merge.new_version_id)
    assert body["llm_draft"]["available"] is True
    assert body["llm_draft"]["model"] == "qwen-test"

    rows = {row["section_path"]: row for row in body["section_classifications"]}
    assert set(rows) == {"Điều 6", "Điều 7"}
    assert rows["Điều 6"]["kind"] == "amended"
    assert rows["Điều 6"]["bucket"] == "amended"
    assert rows["Điều 6"]["consolidated_text"].endswith("5%.")
    assert rows["Điều 7"]["kind"] == "added"
    # A section the drafter did not write is shown as the incoming text, marked undrafted.
    assert rows["Điều 7"]["drafted"] is False


def test_the_middle_pane_carries_a_word_level_diff(client: TestClient, merge: Merge) -> None:
    """A reviewer comparing "3%" and "5%" by eye across two panes will miss one."""
    body = screen(client, merge.task_id).json()
    row = next(r for r in body["section_classifications"] if r["section_path"] == "Điều 6")
    inserted = [span["text"] for span in row["spans"] if span["op"] == "insert"]
    deleted = [span["text"] for span in row["spans"] if span["op"] == "delete"]
    assert any("5%" in text for text in inserted)
    assert any("3%" in text for text in deleted)


def test_the_screen_says_how_many_approvals_are_still_needed(
    client: TestClient, merge: Merge
) -> None:
    body = screen(client, merge.task_id).json()
    assert body["approvals"]["required"] == 2
    assert body["approvals"]["received"] == 0
    assert body["approvals"]["satisfied"] is False
    assert body["touched_articles"] == [6, 7]


def test_a_reviewer_outside_the_legal_cell_cannot_see_the_merge(
    client: TestClient, merge: Merge
) -> None:
    """Not 403: that a consolidation is under way is itself information."""
    response = client.get(
        f"/v1/merge-tasks/{merge.task_id}",
        headers={"Authorization": f"Bearer {make_token('u-retail', ['/dept/retail'])}"},
    )
    assert response.status_code == 404


def test_an_anonymous_caller_sees_nothing(client: TestClient, merge: Merge) -> None:
    assert client.get(f"/v1/merge-tasks/{merge.task_id}").status_code == 401


def test_a_missing_draft_is_reported_rather_than_rendered_as_an_empty_pane(
    client: TestClient, session: Session, store: LocalStorageAdapter
) -> None:
    without = Merge(session, store).build(with_draft=False)
    body = screen(client, without.task_id).json()
    assert body["llm_draft"]["available"] is False
    # The diff still stands on its own: the reviewer can work without the model.
    assert body["section_classifications"]
    assert all(row["inferred"] for row in body["section_classifications"])


# -------------------------------------------------------------------------- the approvals


def test_two_approvals_publish_and_the_workflow_is_told(
    client: TestClient, merge: Merge, workflows: InMemoryStarter
) -> None:
    first = decide(client, merge.task_id, user="u-legal-one", draft_ref=merge.draft_ref)
    assert first.status_code == 200
    assert first.json()["satisfied"] is False
    assert first.json()["received"] == 1

    second = decide(
        client,
        merge.task_id,
        user="u-legal-two",
        note="Đã đối chiếu Điều 6.",
        draft_ref=merge.draft_ref,
    )
    assert second.json()["satisfied"] is True
    assert second.json()["signalled"] is True

    signals = [(name, payload["approver"]) for _, name, payload in workflows.signals]
    assert signals == [("approve", "u-legal-one"), ("approve", "u-legal-two")]


def test_the_preparer_cannot_approve_their_own_consolidation(
    client: TestClient, merge: Merge, workflows: InMemoryStarter
) -> None:
    response = decide(client, merge.task_id, user=PREPARER, draft_ref=merge.draft_ref)
    assert response.status_code == 409
    assert "preparer" in response.json()["message"]
    assert not workflows.signals, "a refused approval must not reach the workflow"


def test_the_same_person_cannot_approve_twice(client: TestClient, merge: Merge) -> None:
    assert decide(client, merge.task_id, user="u-legal-one").status_code == 200
    again = decide(client, merge.task_id, user="u-legal-one")
    assert again.status_code == 409
    assert "already approved" in again.json()["message"]


def test_an_approval_for_a_redrafted_text_is_refused(client: TestClient, merge: Merge) -> None:
    """They read a draft that no longer exists; approving it would approve nothing."""
    response = decide(client, merge.task_id, user="u-legal-one", draft_ref="kb-derived/stale")
    assert response.status_code == 409
    assert "draft changed" in response.json()["message"]


def test_a_rejection_closes_the_merge(
    client: TestClient, merge: Merge, workflows: InMemoryStarter
) -> None:
    response = decide(
        client,
        merge.task_id,
        user="u-legal-one",
        decision="reject",
        note="Bản hợp nhất bỏ sót Khoản 3.",
    )
    assert response.status_code == 200
    assert workflows.signals[0][1] == "reject"

    # And nothing further is accepted against it.
    assert decide(client, merge.task_id, user="u-legal-two").status_code == 409


def test_an_operational_merge_needs_one_approver(
    client: TestClient, session: Session, store: LocalStorageAdapter
) -> None:
    operational = Merge(session, store).build(doc_class="operational")
    response = decide(client, operational.task_id, user="u-legal-one")
    assert response.json()["required"] == 1
    assert response.json()["satisfied"] is True


def test_an_undeliverable_signal_still_records_the_decision(
    client: TestClient, merge: Merge, workflows: InMemoryStarter, session: Session
) -> None:
    """A Temporal outage must not lose an approval a person gave."""
    workflows.statuses.clear()  # the workflow is unknown to the starter
    response = decide(client, merge.task_id, user="u-legal-one", draft_ref=merge.draft_ref)
    assert response.status_code == 200
    assert response.json()["signalled"] is False
    assert response.json()["received"] == 1

    session.expire_all()
    task = session.get(ReviewTaskRow, merge.task_id)
    assert task is not None
    assert [item["approver"] for item in task.payload["approvals"]] == ["u-legal-one"]
