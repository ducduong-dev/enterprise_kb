"""Identity matching, section diffing and the merge draft."""

from __future__ import annotations

import json

import pytest
from kb_identity_merge.diff import ChangeKind, DocumentDiff, diff_documents, word_diff
from kb_identity_merge.matching import Candidate, MatchDecision, match
from kb_identity_merge.merge import Bucket, MergeDrafter
from kb_idp.builder import KBDocBuilder
from kb_ports.adapters.generation import ScriptedGeneration
from kb_schemas.kbdoc import KBDoc

TT41 = Candidate(
    document_id="doc-tt41",
    legal_number="41/2016/TT-NHNN",
    title="Thông tư quy định tỷ lệ an toàn vốn đối với ngân hàng",
)
QD1627 = Candidate(
    document_id="doc-qd1627",
    legal_number="1627/2001/QĐ-NHNN",
    title="Quyết định ban hành Quy chế cho vay của tổ chức tín dụng",
)
POLICY = Candidate(
    document_id="doc-policy",
    legal_number=None,
    title="Chính sách quản lý vốn nội bộ",
)
CANDIDATES = [TT41, QD1627, POLICY]


# ------------------------------------------------------------------------------- matching


def test_an_exact_legal_number_is_the_same_instrument() -> None:
    result = match(
        legal_number="41/2016/TT-NHNN", title="Thông tư an toàn vốn", candidates=CANDIDATES
    )
    assert result.decision is MatchDecision.SAME_INSTRUMENT
    assert result.candidate is TT41
    assert result.layer == "exact_number"
    assert result.is_automatic


def test_diacritics_in_the_instrument_code_do_not_create_a_second_document() -> None:
    """QĐ and QD are one instrument typed two ways, and OCR produces both."""
    result = match(
        legal_number="1627/2001/QD-NHNN",
        title="Quyết định ban hành Quy chế cho vay",
        candidates=CANDIDATES,
    )
    assert result.decision is MatchDecision.SAME_INSTRUMENT
    assert result.candidate is QD1627
    assert result.layer == "normalized_number"


def test_a_misread_number_is_proposed_never_decided() -> None:
    """A number recovered from a bad scan is a hypothesis, not an identity."""
    result = match(
        legal_number="41/2Ol6/TT-NHNN",
        title="Thông tư quy định tỷ lệ an toàn vốn đối với ngân hàng",
        candidates=CANDIDATES,
    )
    assert result.decision is MatchDecision.NEEDS_REVIEW
    assert result.candidate is TT41
    assert result.layer == "fuzzy_number"
    assert not result.is_automatic


def test_a_similar_number_with_an_unrelated_title_is_not_proposed() -> None:
    """Numbers collide; the title is what stops two unrelated instruments merging."""
    result = match(
        legal_number="41/2016/TT-NHNM",
        title="Hướng dẫn vận hành quầy giao dịch chi nhánh",
        candidates=CANDIDATES,
    )
    assert result.decision is MatchDecision.NEW_DOCUMENT


def test_a_document_with_no_number_matches_only_on_a_near_identical_title() -> None:
    same = match(legal_number=None, title="Chính sách quản lý vốn nội bộ", candidates=CANDIDATES)
    assert same.decision is MatchDecision.NEEDS_REVIEW
    assert same.candidate is POLICY

    different = match(
        legal_number=None, title="Chính sách bảo mật thông tin khách hàng", candidates=CANDIDATES
    )
    assert different.decision is MatchDecision.NEW_DOCUMENT


def test_boilerplate_in_a_title_does_not_carry_the_match() -> None:
    """Every instrument says "Thông tư quy định về..."; that cannot be the signal."""
    result = match(
        legal_number=None,
        title="Thông tư quy định về việc ban hành một số điều",
        candidates=CANDIDATES,
    )
    assert result.decision is MatchDecision.NEW_DOCUMENT


def test_an_empty_registry_yields_a_new_document() -> None:
    result = match(legal_number="99/2026/TT-NHNN", title="Thông tư mới", candidates=[])
    assert result.decision is MatchDecision.NEW_DOCUMENT


def test_the_decision_records_which_layer_made_it() -> None:
    """The review task shows the reviewer why the platform thinks these are the same."""
    result = match(
        legal_number="41/2Ol6/TT-NHNN",
        title="Thông tư quy định tỷ lệ an toàn vốn đối với ngân hàng",
        candidates=CANDIDATES,
    )
    payload = result.as_dict()
    assert payload["layer"] == "fuzzy_number"
    assert "similar" in str(payload["reason"])
    assert payload["candidate_number"] == "41/2016/TT-NHNN"


# ----------------------------------------------------------------------------- diffing


def circular(article_six: str, *, extra: str | None = None, drop_twelve: bool = False) -> KBDoc:
    builder = KBDocBuilder(source_format="docx")
    builder.add("Chương II. TỶ LỆ AN TOÀN VỐN", block_type="heading")
    builder.add("Điều 6. Tỷ lệ an toàn vốn", block_type="heading")
    builder.add(article_six)
    if not drop_twelve:
        builder.add("Điều 12. Tài sản có rủi ro tín dụng", block_type="heading")
        builder.add("Tài sản có rủi ro tín dụng được xác định theo phương pháp tiêu chuẩn.")
    if extra:
        builder.add("Điều 12a. Quy định chuyển tiếp", block_type="heading")
        builder.add(extra)
    return builder.build(page_count=1)


ORIGINAL = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8%."
AMENDED = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 10%."


def test_an_amended_article_is_reported_as_amended() -> None:
    result = diff_documents(circular(ORIGINAL), circular(AMENDED))
    change = next(c for c in result.changed if "Điều 6" in c.section_path)
    assert change.kind is ChangeKind.AMENDED
    assert change.similarity > 0.9
    assert result.touched_articles == [6]


def test_an_unchanged_article_is_not_reported_as_a_change() -> None:
    result = diff_documents(circular(ORIGINAL), circular(ORIGINAL))
    assert result.changed == []
    assert result.summary()["unchanged"] >= 2


def test_reflowed_whitespace_is_not_an_amendment() -> None:
    """A re-typed paragraph with different line breaks has not changed the law."""
    result = diff_documents(circular(ORIGINAL), circular(f"{ORIGINAL}\n"))
    assert result.changed == []


def test_an_inserted_article_does_not_report_everything_after_it_as_changed() -> None:
    """The reason the diff aligns on structure rather than position."""
    result = diff_documents(
        circular(ORIGINAL), circular(ORIGINAL, extra="Quy định này áp dụng từ 01/01/2027.")
    )
    assert [c.kind for c in result.changed] == [ChangeKind.ADDED]
    assert result.changed[0].section_path.endswith("Điều 12a")


def test_a_removed_article_is_reported_as_removed() -> None:
    result = diff_documents(circular(ORIGINAL), circular(ORIGINAL, drop_twelve=True))
    removed = [c for c in result.changed if c.kind is ChangeKind.REMOVED]
    assert removed
    assert 12 in {c.article for c in removed}


def test_a_completely_different_text_is_a_rewrite_not_an_edit() -> None:
    """A word-level diff of two unrelated texts teaches a reviewer nothing."""
    result = diff_documents(
        circular(ORIGINAL), circular("Chi nhánh ngân hàng nước ngoài báo cáo hằng quý.")
    )
    change = next(c for c in result.changed if "Điều 6" in c.section_path)
    assert change.kind is ChangeKind.REWRITTEN, (
        "a shared heading must not make two unrelated bodies look like an edit"
    )
    assert change.spans == ()


def test_the_word_diff_marks_what_moved() -> None:
    spans = word_diff(ORIGINAL, AMENDED)
    assert any(span.op == "delete" and "8%" in span.text for span in spans)
    assert any(span.op == "insert" and "10%" in span.text for span in spans)
    assert any(span.op == "equal" for span in spans)


def test_touched_articles_drive_the_impact_filter() -> None:
    result = diff_documents(circular(ORIGINAL), circular(AMENDED))
    assert result.touched_articles == [6]
    assert 12 not in result.touched_articles


# ------------------------------------------------------------------------- merge draft


def classifier_response() -> str:
    return json.dumps(
        {
            "sections": [
                {
                    "idx": 0,
                    "bucket": "amended",
                    "impact": "Tỷ lệ an toàn vốn tối thiểu tăng từ 8% lên 10%.",
                    "confidence": 0.94,
                }
            ]
        }
    )


def drafter_response() -> str:
    return json.dumps(
        {
            "section_path": "Chương II > Điều 6",
            "consolidated_text": f"Điều 6. Tỷ lệ an toàn vốn\n{AMENDED}",
            "note": "áp dụng sửa đổi tại Thông tư 22/2023/TT-NHNN",
        }
    )


def test_the_draft_classifies_and_consolidates_each_change() -> None:
    model = ScriptedGeneration(responses=[classifier_response(), drafter_response()])
    diff = diff_documents(circular(ORIGINAL), circular(AMENDED))
    draft = MergeDrafter(model).draft(diff)

    assert draft.complete
    assert draft.substantive_changes == 1
    assert draft.by_bucket(Bucket.AMENDED)[0].impact.startswith("Tỷ lệ an toàn vốn")
    assert "10%" in draft.sections[0].consolidated_text


def test_the_drafter_sees_one_section_at_a_time() -> None:
    """A model handed two whole circulars improves prose nobody asked it to touch."""
    model = ScriptedGeneration(responses=[classifier_response(), drafter_response()])
    MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    drafting_prompt = model.calls[-1][0].content
    assert ORIGINAL in drafting_prompt
    assert AMENDED in drafting_prompt
    assert "Tài sản có rủi ro tín dụng" not in drafting_prompt


def test_an_added_article_needs_no_model_call() -> None:
    model = ScriptedGeneration(
        responses=[
            json.dumps(
                {
                    "sections": [
                        {
                            "idx": 0,
                            "bucket": "new_or_abrogated",
                            "impact": "Bổ sung quy định chuyển tiếp.",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        ]
    )
    diff = diff_documents(circular(ORIGINAL), circular(ORIGINAL, extra="Áp dụng từ 01/01/2027."))
    draft = MergeDrafter(model).draft(diff)

    assert len(model.calls) == 1, "the classifier only; a new article is already consolidated"
    # The section is heading plus body, which is the unit a reviewer approves.
    assert draft.sections[0].consolidated_text.endswith("Áp dụng từ 01/01/2027.")
    assert "Điều 12a" in draft.sections[0].consolidated_text


def test_a_removed_article_drafts_to_an_empty_consolidated_text() -> None:
    model = ScriptedGeneration(responses=[json.dumps({"sections": []})])
    diff = diff_documents(circular(ORIGINAL), circular(ORIGINAL, drop_twelve=True))
    draft = MergeDrafter(model).draft(diff)
    removed = [s for s in draft.sections if s.consolidated_text == ""]
    assert removed
    assert "bãi bỏ" in removed[0].note


def test_a_failed_classification_falls_back_to_the_diffs_own_verdict() -> None:
    """And falls back conservatively: unclassified means the reviewer reads it."""
    model = ScriptedGeneration(responses=["not json", drafter_response()])
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    assert not draft.complete
    assert draft.classifications[0].bucket is Bucket.AMENDED
    assert draft.classifications[0].inferred


def test_a_failed_draft_shows_the_amendment_text_and_says_so() -> None:
    """An incomplete draft presented as complete is worse than no draft."""
    model = ScriptedGeneration(responses=[classifier_response(), "the model fell over"])
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    assert not draft.complete
    assert not draft.sections[0].drafted
    assert AMENDED in draft.sections[0].consolidated_text
    assert "không tạo được" in draft.sections[0].note


def test_a_section_the_model_skipped_is_flagged_not_dropped() -> None:
    model = ScriptedGeneration(responses=[json.dumps({"sections": []}), drafter_response()])
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))
    assert draft.classifications[0].inferred
    assert not draft.complete


def test_an_unrecognised_bucket_is_treated_as_substantive() -> None:
    model = ScriptedGeneration(
        responses=[
            json.dumps(
                {
                    "sections": [
                        {
                            "idx": 0,
                            "bucket": "probably fine",
                            "impact": "",
                            "confidence": 0.9,
                        }
                    ]
                }
            ),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))
    assert draft.classifications[0].bucket is Bucket.AMENDED


# --------------------------------------------------------- indexed model I/O (ADR-0034)


def _classifier_reply(*sections: dict[str, object]) -> str:
    return json.dumps({"sections": list(sections)})


def test_the_classifier_numbers_the_sections_it_sends() -> None:
    """The paths stay in the prompt as labels; the index is what the answer travels on."""
    model = ScriptedGeneration(responses=[classifier_response(), drafter_response()])
    MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    classifier_prompt = model.calls[0][0].content
    assert "[0] Chương II > Điều 6" in classifier_prompt


def test_a_retyped_section_path_no_longer_loses_a_classification() -> None:
    """The v1 failure: the model re-types the path, the lookup misses, and a real verdict is
    thrown away as though the section had never been classified."""
    model = ScriptedGeneration(
        responses=[
            _classifier_reply(
                {
                    "idx": 0,
                    "section_path": "Chuong II, dieu 6",  # diacritics dropped, as OCR does
                    "bucket": "unchanged_in_substance",
                    "impact": "Chỉ thay đổi cách diễn đạt.",
                    "confidence": 0.8,
                }
            ),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    assert draft.classifications[0].bucket is Bucket.UNCHANGED_IN_SUBSTANCE
    assert not draft.classifications[0].inferred
    assert draft.classifications[0].section_path == "Chương II > Điều 6"
    assert draft.complete


def test_an_out_of_range_index_fails_the_whole_call() -> None:
    """Not dropped and not repaired: a model that answers about item 7 of a one-item list did
    not understand the question, and its other answers are not evidence that it did."""
    model = ScriptedGeneration(
        responses=[
            _classifier_reply(
                {"idx": 0, "bucket": "unchanged_in_substance", "impact": "", "confidence": 0.9},
                {"idx": 7, "bucket": "amended", "impact": "", "confidence": 0.9},
            ),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    assert not draft.complete
    assert all(item.inferred for item in draft.classifications)
    # And conservatively: the in-range verdict said "cosmetic", which is not what the reviewer
    # is shown, because the call it arrived in failed.
    assert draft.classifications[0].bucket is Bucket.AMENDED


def test_a_duplicated_index_fails_the_whole_call() -> None:
    model = ScriptedGeneration(
        responses=[
            _classifier_reply(
                {"idx": 0, "bucket": "unchanged_in_substance", "impact": "", "confidence": 0.9},
                {"idx": 0, "bucket": "amended", "impact": "", "confidence": 0.9},
            ),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    assert not draft.complete
    assert draft.classifications[0].inferred


def test_a_verdict_with_no_index_fails_the_call() -> None:
    """v1's silent-corruption case: an item matching nothing sat in the map, inflating the
    count that `complete` was computed from."""
    model = ScriptedGeneration(
        responses=[
            _classifier_reply({"bucket": "amended", "impact": "", "confidence": 0.9}),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(AMENDED)))

    assert not draft.complete
    assert draft.classifications[0].inferred


def _two_section_diff() -> DocumentDiff:
    diff = diff_documents(circular(ORIGINAL), circular(AMENDED, extra="Áp dụng từ 01/01/2027."))
    assert len(diff.changed) == 2, "fixture must offer the model two sections to answer about"
    return diff


def test_a_skipped_index_is_a_detected_omission_not_a_lost_answer() -> None:
    """The verdict the model did give survives; only the one it omitted falls back."""
    model = ScriptedGeneration(
        responses=[
            _classifier_reply(
                {"idx": 1, "bucket": "new_or_abrogated", "impact": "", "confidence": 0.9}
            ),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(_two_section_diff())

    assert not draft.complete
    assert draft.classifications[0].inferred
    assert not draft.classifications[1].inferred
    assert draft.classifications[1].bucket is Bucket.NEW_OR_ABROGATED


def test_completeness_counts_answers_not_items_returned() -> None:
    """v1's corrupted honesty signal, exactly: a reply that names one real section and one
    imaginary one, while omitting a real section, counted as two answers over two changes and
    reported `complete=True`. `MergeDraft.complete` exists to tell a reviewer the draft is
    partial, and in that case it said the opposite."""
    model = ScriptedGeneration(
        responses=[
            _classifier_reply(
                {"idx": 1, "bucket": "unchanged_in_substance", "impact": "", "confidence": 0.9},
                {"idx": 5, "bucket": "unchanged_in_substance", "impact": "", "confidence": 0.9},
            ),
            drafter_response(),
        ]
    )
    draft = MergeDrafter(model).draft(_two_section_diff())

    assert not draft.complete
    assert all(item.inferred for item in draft.classifications)


def test_a_document_with_no_changes_needs_no_model_at_all() -> None:
    model = ScriptedGeneration(responses=[])
    draft = MergeDrafter(model).draft(diff_documents(circular(ORIGINAL), circular(ORIGINAL)))
    assert draft.complete
    assert model.calls == []


@pytest.mark.parametrize("bucket", list(Bucket))
def test_every_bucket_the_plan_names_exists(bucket: Bucket) -> None:
    assert bucket.value in {
        "unchanged_in_substance",
        "amended",
        "new_or_abrogated",
    }
