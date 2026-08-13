"""Embedding and rerank adapters that run without a GPU node."""

from __future__ import annotations

import math

from kb_ports.adapters.embedding_hashed import DIMENSIONS, HashedEmbeddingAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter

VN = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8%."
VN_RELATED = "Tỷ lệ an toàn vốn được xác định theo phương pháp tiêu chuẩn."
UNRELATED = "Giao dịch viên đối chiếu giấy tờ tùy thân khi mở tài khoản."


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_vectors_are_unit_length_and_the_configured_width() -> None:
    """The index column is fixed at 1024; a mismatched vector cannot be stored."""
    vector = HashedEmbeddingAdapter().embed_query(VN)
    assert len(vector) == DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_embedding_is_deterministic_across_instances() -> None:
    """CI reruns and multiple workers must produce byte-identical vectors."""
    assert HashedEmbeddingAdapter().embed_query(VN) == HashedEmbeddingAdapter().embed_query(VN)


def test_related_text_scores_above_unrelated_text() -> None:
    adapter = HashedEmbeddingAdapter()
    query = adapter.embed_query("tỷ lệ an toàn vốn tối thiểu")
    (related, unrelated) = adapter.embed_documents([VN_RELATED, UNRELATED])
    assert cosine(query, related) > cosine(query, unrelated)


def test_queries_without_diacritics_still_match() -> None:
    """Users type "an toan von" constantly; the folded form is hashed alongside."""
    adapter = HashedEmbeddingAdapter()
    folded = adapter.embed_query("ty le an toan von")
    (related, unrelated) = adapter.embed_documents([VN_RELATED, UNRELATED])
    assert cosine(folded, related) > cosine(folded, unrelated)


def test_empty_text_produces_a_usable_vector() -> None:
    """A zero vector has undefined cosine distance and would poison an HNSW index."""
    vector = HashedEmbeddingAdapter().embed_query("")
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_batch_and_single_paths_agree() -> None:
    adapter = HashedEmbeddingAdapter()
    assert adapter.embed_documents([VN])[0] == adapter.embed_query(VN)


def test_adapter_declares_that_it_is_not_semantic() -> None:
    """A reader of an eval report must be able to tell which model produced the numbers."""
    assert HashedEmbeddingAdapter().info.extra["semantic"] is False
    assert LexicalRerankAdapter().info.extra["semantic"] is False


# ----------------------------------------------------------------------------- reranking


def test_reranker_puts_the_answering_passage_first() -> None:
    ranked = LexicalRerankAdapter().rerank(
        "tỷ lệ an toàn vốn tối thiểu", [UNRELATED, VN_RELATED, VN]
    )
    assert ranked[0].index in (1, 2)
    assert ranked[-1].index == 0


def test_reranker_returns_every_candidate_unless_truncated() -> None:
    """A reranker that drops candidates silently loses answers."""
    passages = [VN, VN_RELATED, UNRELATED]
    ranked = LexicalRerankAdapter().rerank("vốn", passages)
    assert sorted(item.index for item in ranked) == [0, 1, 2]
    assert len(LexicalRerankAdapter().rerank("vốn", passages, top_k=2)) == 2


def test_reranking_is_stable_for_equal_scores() -> None:
    ranked = LexicalRerankAdapter().rerank("không khớp gì cả", ["a a a", "b b b", "c c c"])
    assert [item.index for item in ranked] == [0, 1, 2]


def test_reranking_an_empty_candidate_set_is_not_an_error() -> None:
    assert LexicalRerankAdapter().rerank("bất kỳ", []) == []
