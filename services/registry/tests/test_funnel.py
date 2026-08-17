"""The funnel that drives the gates over a corpus (M9d, ADR-0033).

Three things are worth testing here and none of them is "does the model give the right answer" —
that is gate 5's own test and the fixture eval. This is about *which pairs get formed at all*,
*what survives a second run*, and *what a proposal does to the queue*.

The model is scripted throughout, so every assertion about cost is exact: `scripted.calls`
counts what the funnel actually spent, and a run that should spend nothing scripts nothing and
would raise if it tried.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from kb_ports.adapters.generation import ScriptedGeneration
from kb_registry.adjudicate import ClauseAdjudicator
from kb_registry.funnel import ClauseFunnel
from kb_registry.supersession import ClauseRef, ClauseSupersessions
from kb_registry.testing import make_chunk, make_document, make_version
from kb_schemas.enums import SupersessionBasis, SupersessionVerdict
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

Y2023 = date(2023, 1, 1)
Y2026 = date(2026, 1, 1)

#: Two shared tokens, which is what the corpus-wide path (C) demands. One token in common is
#: "lãi" and means nothing.
SUBJECT = "lai suat cho vay ngan han"
OLD_TEXT = "Lãi suất cho vay ngắn hạn là 8%/năm."
NEW_TEXT = "Lãi suất cho vay ngắn hạn là 10%/năm."


def reply(verdict: str, *, replacement_idx: int | None = None) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "replacement_idx": replacement_idx,
            "confidence": 0.9,
            "rationale": "mức lãi suất đã thay đổi",
        }
    )


def make_clause(
    session: Session,
    *,
    title: str,
    body: str,
    effective_from: date,
    legal_number: str | None = None,
    subject_key: str = SUBJECT,
    section_path: str = "Điều 5",
) -> tuple[uuid.UUID, ClauseRef]:
    """One published document holding exactly one clause, so a pair is a pair."""
    document_id = make_document(session, title=title, legal_number=legal_number)
    version_id = make_version(session, document_id, effective_from=effective_from)
    make_chunk(
        session,
        document_id,
        version_id,
        effective_from=effective_from,
        section_path=section_path,
        body=body,
        subject_key=subject_key,
    )
    session.flush()
    return document_id, ClauseRef(document_id, section_path)


def two_documents(session: Session, **kwargs: object) -> tuple[uuid.UUID, uuid.UUID]:
    old, _ = make_clause(session, title="Thông tư 01/2023", body=OLD_TEXT, effective_from=Y2023)
    new, _ = make_clause(session, title="Thông tư 09/2026", body=NEW_TEXT, effective_from=Y2026)
    return old, new


def edge(session: Session, src: uuid.UUID, dst: uuid.UUID, *, articles: list[int] | None) -> None:
    session.execute(
        text(
            "INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type, "
            "articles, detected_by, created_at) "
            "VALUES (:id, :src, :dst, 'amends', :articles, 'test', now())"
        ),
        {"id": uuid.uuid4(), "src": src, "dst": dst, "articles": articles},
    )
    session.flush()


def funnel(session: Session, *responses: str) -> tuple[ClauseFunnel, ScriptedGeneration]:
    scripted = ScriptedGeneration(responses=list(responses))
    return ClauseFunnel(session, ClauseAdjudicator(scripted)), scripted


def verdict_rows(session: Session) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in session.execute(
            text(
                "SELECT verdict, settled_by, text_digest, left_section_path, right_section_path "
                "FROM clause_pair_verdicts"
            )
        ).mappings()
    ]


def tasks(session: Session) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in session.execute(
            text("SELECT version_id, payload FROM review_tasks WHERE task_type = 'clause_review'")
        ).mappings()
    ]


# ------------------------------------------------------------------------------- the paths


def test_an_edge_naming_articles_is_never_second_guessed(session: Session) -> None:
    """Path A. The corpus already said which articles moved and ADR-0032 flags exactly those,
    so the funnel must not form the pair at all — a machine arguing with a confirmed edge is
    the failure, not the disagreement."""
    old, new = two_documents(session)
    edge(session, new, old, articles=[5])

    runner, scripted = funnel(session)
    report = runner.run(new)

    assert report.pairs == 0
    assert report.skipped_neighbours == {old}
    assert scripted.calls == []
    assert verdict_rows(session) == []


def test_an_edge_without_articles_searches_the_two_documents(session: Session) -> None:
    """Path B. The documents are known to be related, so the search space is two documents and
    the subject gate relaxes to any shared token."""
    old, new = two_documents(session)
    edge(session, new, old, articles=None)
    # Exactly one token in common ("lai") — below Path C's threshold of two, which is
    # what the relaxation exists for.
    session.execute(
        text("UPDATE chunks SET subject_key = 'lai phat cham tra' WHERE document_id = :d"),
        {"d": old},
    )
    session.flush()

    runner, scripted = funnel(session, reply("superseded", replacement_idx=0))
    report = runner.run(new)

    assert report.pairs == 1
    assert report.outcomes[0].path == "B"
    assert len(scripted.calls) == 1


def test_no_edge_searches_the_corpus_and_wants_a_real_subject_overlap(session: Session) -> None:
    """Path C, the case the funnel was built for: a later instrument re-states a rule and never
    mentions the earlier one."""
    _, new = two_documents(session)

    runner, _ = funnel(session, reply("superseded", replacement_idx=0))
    report = runner.run(new)

    assert report.pairs == 1
    assert report.outcomes[0].path == "C"
    assert report.by_model == 1 and report.by_gate == 0


def test_a_thin_subject_overlap_forms_no_pair_without_an_edge(session: Session) -> None:
    """One shared token is "lãi" and means nothing. Path C is the strict gate precisely because
    it is looking at 3,000 documents rather than at one known neighbour."""
    old, new = two_documents(session)
    session.execute(
        text("UPDATE chunks SET subject_key = 'lai phat cham tra' WHERE document_id = :d"),
        {"d": old},
    )
    session.flush()

    runner, scripted = funnel(session)
    assert runner.run(new).pairs == 0
    assert scripted.calls == []


# -------------------------------------------------------------------------- what a run writes


def test_a_supersession_is_proposed_and_lands_in_a_queue(session: Session) -> None:
    """The proposal, the pair record and the steward task, and nothing anyone can see change."""
    old, new = two_documents(session)
    runner, _ = funnel(session, reply("superseded", replacement_idx=0))

    report = runner.run(new)

    assert report.proposals == 1
    row = session.execute(
        text(
            "SELECT basis, state, verdict, old_document_id, new_document_id, supersedes_from, "
            "detected_by FROM clause_supersessions"
        )
    ).mappings()
    record = next(iter(row))
    # Direction from the legal dates, not from which document the run started at.
    assert record["old_document_id"] == old and record["new_document_id"] == new
    assert record["supersedes_from"] == Y2026
    assert record["state"] == "proposed"
    # `detected` and never `edge_article`: on Path B the edge said the documents are related,
    # not which clauses moved, so the pairing is still our inference.
    assert record["basis"] == SupersessionBasis.DETECTED.value

    opened = tasks(session)
    assert len(opened) == 1
    assert opened[0]["payload"]["old"]["document_id"] == str(old)
    # What makes the queue workable: the delta, not a similarity score.
    assert opened[0]["payload"]["quantity_delta"] == {"changed": ["8%/năm → 10%/năm"]}

    # Proposed changes nothing that is served.
    assert ClauseSupersessions(session).served({old}) == {}


@pytest.mark.parametrize(
    "name", ["same_rule_restated", "different_scope", "conflicting_unresolved"]
)
def test_the_other_three_verdicts_are_recorded_and_propose_nothing(
    session: Session, name: str
) -> None:
    """ "Recorded as such" is an M9d acceptance criterion, and `clause_supersessions` cannot hold
    them — a null replacement there is a pure abrogation, the opposite of what they mean.

    Parametrized rather than looped: each case wants a corpus of exactly two documents, and a
    loop leaves the previous iteration's pair behind for gate 1 to find."""
    _, new = two_documents(session)
    runner, _ = funnel(session, reply(name))

    report = runner.run(new)

    assert report.pairs == 1
    assert report.proposals == 0
    rows = verdict_rows(session)
    assert len(rows) == 1 and rows[0]["verdict"] == name
    assert session.execute(text("SELECT count(*) FROM clause_supersessions")).scalar() == 0
    assert tasks(session) == []


def test_a_declared_answer_is_never_overwritten_by_a_detection(session: Session) -> None:
    """The declared path is stronger evidence by construction (ADR-0040). A detection that
    proposed over it would close a row a person may already have confirmed."""
    old_doc, new_doc = two_documents(session)
    declared = ClauseSupersessions(session)
    declared.propose(
        ClauseRef(old_doc, "Điều 5"),
        new=ClauseRef(new_doc, "Điều 5"),
        supersedes_from=Y2026,
        basis=SupersessionBasis.DECLARED,
        detected_by="declaration_reader",
        actor="steward@bank",
        evidence="Điều 5 Thông tư này thay thế Điều 5 Thông tư 01/2023.",
    )
    session.flush()

    runner, _ = funnel(session, reply("superseded", replacement_idx=0))
    report = runner.run(new_doc)

    assert report.proposals == 0
    assert session.execute(text("SELECT count(*) FROM clause_supersessions")).scalar() == 1
    # The adjudication still happened and is still recorded — we looked, and we agree.
    assert len(verdict_rows(session)) == 1


# ------------------------------------------------------------------------------ running twice


def test_a_second_run_costs_nothing_and_proposes_nothing_twice(session: Session) -> None:
    """The property that makes a 3,000-document backfill resumable rather than one transaction
    nobody dares restart."""
    _, new = two_documents(session)
    runner, scripted = funnel(session, reply("superseded", replacement_idx=0))
    runner.run(new)
    assert len(scripted.calls) == 1

    again, second = funnel(session)
    report = again.run(new)

    assert report.pairs == 1
    assert report.outcomes[0].from_cache
    assert second.calls == []
    assert session.execute(text("SELECT count(*) FROM clause_supersessions")).scalar() == 1


def test_a_reworded_clause_is_adjudicated_again(session: Session) -> None:
    """`text_digest` is what keeps a stored verdict honest. Without it the table would answer
    for text that no longer exists — the same failure ADR-0035's cache key is shaped to
    prevent, and the reason neither keys on an id."""
    _, new = two_documents(session)
    runner, _ = funnel(session, reply("different_scope"))
    runner.run(new)

    session.execute(
        text("UPDATE chunks SET text = :t WHERE document_id = :d"),
        {"t": "Lãi suất cho vay ngắn hạn là 12%/năm.", "d": new},
    )
    session.flush()

    again, second = funnel(session, reply("superseded", replacement_idx=0))
    report = again.run(new)

    assert not report.outcomes[0].from_cache
    assert len(second.calls) == 1
    rows = verdict_rows(session)
    # Replaced, not appended: this is a record of work, and the digest already says whether it
    # is current.
    assert len(rows) == 1 and rows[0]["verdict"] == "superseded"


def test_a_pair_has_one_identity_whichever_document_the_run_started_at(session: Session) -> None:
    """A-against-B and B-against-A are one pair. Storing them as two would let a backfill do
    every pair's work twice and would show a steward the same finding from both ends."""
    old, new = two_documents(session)
    first, _ = funnel(session, reply("different_scope"))
    first.run(new)

    second, scripted = funnel(session)
    report = second.run(old)

    assert report.pairs == 1
    assert report.outcomes[0].from_cache
    assert scripted.calls == []
    assert len(verdict_rows(session)) == 1


def test_nothing_is_stored_when_nothing_concluded(session: Session) -> None:
    """A model that was down is not a verdict. Storing it would make the next run skip a pair
    nobody has ever adjudicated."""
    _, new = two_documents(session)
    runner, _ = funnel(session)  # empty script: the call raises, as an unreachable model does

    report = runner.run(new)

    assert report.pairs == 1
    assert report.unresolved == 1
    assert verdict_rows(session) == []

    retried, scripted = funnel(session, reply("superseded", replacement_idx=0))
    assert retried.run(new).proposals == 1
    assert len(scripted.calls) == 1


# --------------------------------------------------------------------------------- ordering


def test_a_run_can_be_capped(session: Session) -> None:
    """`limit` is what lets the backfill proceed in bounded chunks."""
    _, new = two_documents(session)
    make_clause(
        session,
        title="Thông tư 05/2024",
        body="Lãi suất cho vay ngắn hạn là 9%/năm.",
        effective_from=date(2024, 1, 1),
    )

    runner, scripted = funnel(session, reply("different_scope"))
    report = runner.run(new, limit=1)

    assert report.pairs == 1
    assert len(scripted.calls) == 1


def test_direction_ignores_which_document_the_run_started_at(session: Session) -> None:
    """The archive is digitised in whatever order it yields, so a 2023 circular routinely lands
    after the 2026 one. Running the funnel over the *older* document must still record the
    newer clause as the replacement."""
    old, new = two_documents(session)
    runner, _ = funnel(session, reply("superseded", replacement_idx=1))

    report = runner.run(old)

    assert report.proposals == 1
    record = next(
        iter(
            session.execute(
                text("SELECT old_document_id, new_document_id FROM clause_supersessions")
            ).mappings()
        )
    )
    assert record["old_document_id"] == old and record["new_document_id"] == new


def test_a_same_day_pair_is_broken_by_the_instrument_in_the_legal_number(session: Session) -> None:
    """`older_first`'s one tie-break, reached through `documents.legal_number` rather than a
    second column holding a fact the first already carries."""
    make_clause(
        session,
        title="Thông tư 01/2026",
        body=OLD_TEXT,
        effective_from=Y2026,
        legal_number="01/2026/TT-NHNN",
    )
    decree, _ = make_clause(
        session,
        title="Nghị định 88/2026",
        body=NEW_TEXT,
        effective_from=Y2026,
        legal_number="88/2026/NĐ-CP",
    )

    runner, _ = funnel(session, reply("superseded", replacement_idx=None))
    report = runner.run(decree)

    assert report.proposals == 1
    assert report.outcomes[0].adjudication.verdict is SupersessionVerdict.SUPERSEDED
    assert report.outcomes[0].adjudication.new is not None
    assert report.outcomes[0].adjudication.new.document_id == decree
