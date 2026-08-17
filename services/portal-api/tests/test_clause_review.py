"""The clause-review screen and its one decision (M9d, ADR-0033).

Where the funnel's output meets a person. Three things have to hold and the rest is layout:

* a proposal is **visible** to the group that stewards the older clause and to nobody else, and
  a task belonging elsewhere is reported missing rather than forbidden;
* confirming goes through `ClauseSupersessions`, so four-eyes, the append-only chain and the
  two clocks are the ledger's rules and not this screen's;
* rejecting is **recorded**, not deleted — the rejection reason is the only signal the
  detector's false-positive rate can ever be measured from.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from kb_common.config import get_settings, reset_settings_cache
from kb_common.db import get_session
from kb_portal_api.auth import reset_verifier_cache
from kb_portal_api.main import app
from kb_registry.supersession import ClauseRef, ClauseSupersessions
from kb_registry.testing import make_chunk, make_document, make_version
from kb_schemas.enums import ReviewTaskState, ReviewTaskType, SupersessionBasis
from kb_schemas.orm import CategoryRow, ReviewTaskRow
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

#: What the `proposal` fixture hands each test: the task, the ledger row, and both documents.
Proposal = dict[str, uuid.UUID]

CATEGORY = "t_clause_review"
STEWARD_GROUP = "dept/ops"
OTHER_GROUP = "dept/legal"
SECRET = "kb-test-secret"
TOOK_EFFECT = date(2026, 1, 1)

OLD_TEXT = "Phí chuyển tiền trong nước qua kênh quầy là 11.000 đồng mỗi giao dịch."
NEW_TEXT = "Phí chuyển tiền trong nước qua kênh quầy là 15.000 đồng mỗi giao dịch."


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
            "groups": groups if groups is not None else [f"/{STEWARD_GROUP}"],
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
def client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def proposal(session: Session) -> Proposal:
    """What a funnel run leaves behind: a proposed row and a task pointing at it."""
    session.merge(CategoryRow(path=CATEGORY, label="Clause review", steward_group=STEWARD_GROUP))
    session.flush()

    old_doc = make_document(session, title="Biểu phí 2023", category=CATEGORY)
    old_version = make_version(session, old_doc, effective_from=date(2023, 1, 1))
    make_chunk(
        session,
        old_doc,
        old_version,
        section_path="Điều 5",
        body=OLD_TEXT,
        effective_from=date(2023, 1, 1),
    )
    new_doc = make_document(session, title="Biểu phí 2026", category=CATEGORY)
    new_version = make_version(session, new_doc, effective_from=TOOK_EFFECT)
    make_chunk(
        session,
        new_doc,
        new_version,
        section_path="Điều 7",
        body=NEW_TEXT,
        effective_from=TOOK_EFFECT,
    )

    decision = ClauseSupersessions(session).propose(
        ClauseRef(old_doc, "Điều 5"),
        new=ClauseRef(new_doc, "Điều 7"),
        supersedes_from=TOOK_EFFECT,
        basis=SupersessionBasis.DETECTED,
        detected_by="m9d_funnel",
        actor="m9d_funnel",
        verdict=None,
        evidence="mức phí đã thay đổi",
    )
    task = ReviewTaskRow(
        id=uuid.uuid4(),
        version_id=old_version,
        task_type=ReviewTaskType.CLAUSE_REVIEW.value,
        state=ReviewTaskState.OPEN.value,
        assignee_group=STEWARD_GROUP,
        payload={
            "supersession_id": str(decision.row_id),
            "old": {"document_id": str(old_doc), "section_path": "Điều 5"},
            "new": {"document_id": str(new_doc), "section_path": "Điều 7"},
            "quantity_delta": {"changed": ["11000 đồng → 15000 đồng"]},
            "rationale": "mức phí đã thay đổi",
            "settled_by": "model",
            "path": "C",
        },
        created_at=datetime.now(UTC),
    )
    session.add(task)
    session.flush()
    return {"task": task.id, "row": decision.row_id, "old": old_doc, "new": new_doc}


def screen(
    client: TestClient,
    task_id: uuid.UUID,
    *,
    user: str = "u-steward-one",
    groups: list[str] | None = None,
) -> Response:
    response: Response = client.get(
        f"/v1/clause-tasks/{task_id}",
        headers={"Authorization": f"Bearer {make_token(user, groups)}"},
    )
    return response


def decide(
    client: TestClient,
    task_id: uuid.UUID,
    decision: str,
    *,
    note: str = "",
    user: str = "u-steward-one",
) -> Response:
    response: Response = client.post(
        f"/v1/clause-tasks/{task_id}/decision",
        json={"decision": decision, "note": note},
        headers={"Authorization": f"Bearer {make_token(user)}"},
    )
    return response


# ------------------------------------------------------------------------------- the screen


def test_the_screen_shows_both_clauses_whole(client: TestClient, proposal: Proposal) -> None:
    """A truncated pane is a reviewer approving text they did not read."""
    body = screen(client, proposal["task"]).json()

    assert body["old"]["text"] == OLD_TEXT
    assert body["new"]["text"] == NEW_TEXT
    assert body["old"]["document_title"] == "Biểu phí 2023"
    assert body["new"]["document_title"] == "Biểu phí 2026"
    assert not body["old"]["missing"] and not body["new"]["missing"]


def test_the_screen_leads_with_the_change_rather_than_a_score(
    client: TestClient, proposal: Proposal
) -> None:
    """ "11000 đồng → 15000 đồng" is what makes the queue workable; a similarity number is what
    makes a reviewer defer to the machine."""
    body = screen(client, proposal["task"]).json()

    assert body["quantity_delta"] == ["11000 đồng → 15000 đồng"]
    assert body["rationale"] == "mức phí đã thay đổi"
    assert body["settled_by_label"]
    assert body["supersedes_from"] == TOOK_EFFECT.isoformat()
    assert body["state"] == "proposed"


def test_a_task_belonging_to_another_group_is_missing_not_forbidden(
    client: TestClient, proposal: Proposal
) -> None:
    """Whether the bank is reviewing a supersession is itself something an outsider has no
    business learning — the same reasoning the merge screen gives."""
    response = screen(client, proposal["task"], user="u-outsider", groups=[f"/{OTHER_GROUP}"])
    assert response.status_code == 404


def test_a_rechunked_away_clause_is_reported_rather_than_blank(
    client: TestClient, session: Session, proposal: Proposal
) -> None:
    """A pane that silently rendered empty would read as "this clause says nothing", which a
    reviewer could plausibly confirm."""
    from sqlalchemy import text as sql

    session.execute(
        sql("UPDATE chunks SET tombstoned = true WHERE document_id = :d"),
        {"d": proposal["new"]},
    )
    session.flush()

    body = screen(client, proposal["task"]).json()

    assert body["new"]["missing"] is True
    assert body["old"]["missing"] is False


# ----------------------------------------------------------------------------- the decision


def test_confirming_flags_the_clause_and_closes_the_task(
    client: TestClient, session: Session, proposal: Proposal
) -> None:
    response = decide(client, proposal["task"], "confirm")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "confirmed"
    # The ledger is the authority; the screen only asked it.
    served = ClauseSupersessions(session).served({proposal["old"]}, on=TOOK_EFFECT)
    assert (proposal["old"], "Điều 5") in served
    task = session.get(ReviewTaskRow, proposal["task"])
    assert task is not None and task.state == ReviewTaskState.DECIDED.value
    assert task.decided_by == "u-steward-one"


def test_rejecting_needs_a_reason(client: TestClient, proposal: Proposal) -> None:
    """The only signal the detector's false-positive rate can be measured from."""
    assert decide(client, proposal["task"], "reject").status_code == 422


def test_rejecting_records_the_reason_and_serves_the_clause_unflagged(
    client: TestClient, session: Session, proposal: Proposal
) -> None:
    """A rejected proposal is a finding about the detector. Deleting it would lose the only
    record that the pair was ever considered."""
    response = decide(
        client, proposal["task"], "reject", note="hai biểu phí áp dụng cho hai kênh khác nhau"
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "revoked"
    assert ClauseSupersessions(session).served({proposal["old"]}, on=TOOK_EFFECT) == {}
    history = ClauseSupersessions(session).history(proposal["old"])
    assert [row.state for row in history] == ["proposed", "revoked"]
    assert any("kênh khác nhau" in (row.evidence or "") for row in history)


def test_a_decided_task_cannot_be_decided_twice(client: TestClient, proposal: Proposal) -> None:
    assert decide(client, proposal["task"], "confirm").status_code == 200
    assert decide(client, proposal["task"], "confirm").status_code == 422


def test_an_unknown_decision_is_refused(client: TestClient, proposal: Proposal) -> None:
    assert decide(client, proposal["task"], "maybe").status_code == 422


def test_another_groups_reviewer_cannot_decide(client: TestClient, proposal: Proposal) -> None:
    response = client.post(
        f"/v1/clause-tasks/{proposal['task']}/decision",
        json={"decision": "confirm", "note": ""},
        headers={"Authorization": f"Bearer {make_token('u-outsider', [f'/{OTHER_GROUP}'])}"},
    )
    assert response.status_code == 404
