"""Embedding and hybrid retrieval, with a fake embedder so CI never needs Ollama."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from eval.retrieval_eval import load_queries, score_rankings
from text2sql.embedding_retriever import (
    EmbeddingError,
    EmbeddingRetriever,
    FallbackRetriever,
    HybridRetriever,
    OllamaEmbedder,
    build_retriever_factory,
    corpus_fingerprint,
)
from text2sql.retriever import TfidfRetriever

EXAMPLES = [
    {"id": "hydro", "question": "有幾座水力發電廠", "intent": "plant_units"},
    {"id": "reserve", "question": "備轉容量率最低日", "intent": "system_metric"},
    {"id": "outage", "question": "機組歲修排程", "intent": "outage"},
]

# 每個概念一個維度：問句提到哪些概念，向量就在那些維度有值。用來模擬「字面不同、意思相同」。
CONCEPTS = {"水力": 0, "水電": 0, "電廠": 1, "座": 1, "備轉": 2, "歲修": 3, "停機": 3}


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            vector = [0.01] * 4
            for word, axis in CONCEPTS.items():
                if word in text:
                    vector[axis] += 1.0
            vectors.append(vector)
        return vectors


class BrokenEmbedder:
    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("Ollama 沒開")


def test_embedding_retrieval_matches_meaning_not_surface_characters() -> None:
    retriever = EmbeddingRetriever(EXAMPLES, FakeEmbedder())

    top = retriever.retrieve("哪些機組在停機", top_k=3)

    assert top[0].example["id"] == "outage"
    assert top[0].score > top[1].score
    assert math.isclose(retriever.retrieve("有幾座水力發電廠")[0].score, 1.0, rel_tol=1e-9)


def test_embedding_floors_drop_weak_examples() -> None:
    retriever = EmbeddingRetriever(EXAMPLES, FakeEmbedder())

    assert len(retriever.retrieve("歲修", top_k=3)) == 3
    assert [item.example["id"] for item in retriever.retrieve("歲修", top_k=3, min_score=0.5)] == [
        "outage"
    ]
    assert retriever.retrieve("歲修", top_k=0) == []


def test_corpus_vectors_are_cached_and_invalidated_when_the_corpus_changes(tmp_path: Path) -> None:
    cache = tmp_path / "vectors.json"
    embedder = FakeEmbedder()

    EmbeddingRetriever(EXAMPLES, embedder, model="m", cache_path=cache)
    EmbeddingRetriever(EXAMPLES, embedder, model="m", cache_path=cache)
    assert len(embedder.calls) == 1, "第二次應該直接讀快取"
    assert json.loads(cache.read_text(encoding="utf-8"))["fingerprint"] == corpus_fingerprint(
        EXAMPLES, "m"
    )

    changed = [*EXAMPLES, {"id": "new", "question": "新的範例", "intent": "other"}]
    EmbeddingRetriever(changed, embedder, model="m", cache_path=cache)
    EmbeddingRetriever(changed, embedder, model="other-model", cache_path=cache)
    assert len(embedder.calls) == 3


def test_hybrid_fuses_ranks_and_keeps_the_cosine_floor() -> None:
    lexical = TfidfRetriever(EXAMPLES)
    semantic = EmbeddingRetriever(EXAMPLES, FakeEmbedder())
    hybrid = HybridRetriever(lexical, semantic)

    top = hybrid.retrieve("哪些機組在停機歲修", top_k=3)

    assert top[0].example["id"] == "outage"
    assert top[0].score == pytest.approx(2 / 61)
    assert [item.example["id"] for item in hybrid.retrieve("歲修", top_k=3, min_score=0.5)] == [
        "outage"
    ]
    with pytest.raises(ValueError):
        HybridRetriever(TfidfRetriever(EXAMPLES[:2]), semantic)


def test_fallback_uses_ngram_with_its_own_floors_when_ollama_is_down() -> None:
    messages: list[str] = []
    lexical = TfidfRetriever(EXAMPLES)
    fallback = FallbackRetriever(
        lexical,
        lambda: EmbeddingRetriever(EXAMPLES, BrokenEmbedder()),
        lexical_min_score=0.05,
        logger=messages.append,
    )

    top = fallback.retrieve("備轉容量率最低是哪天", top_k=3, min_score=0.9)

    assert [item.example["id"] for item in top] == ["reserve"]
    assert fallback.last_method == "ngram"
    assert "Ollama 沒開" in (fallback.last_error or "")
    assert messages


def test_fallback_builds_the_semantic_index_lazily_once() -> None:
    builds: list[int] = []

    def build() -> EmbeddingRetriever:
        builds.append(1)
        return EmbeddingRetriever(EXAMPLES, FakeEmbedder())

    fallback = FallbackRetriever(TfidfRetriever(EXAMPLES), build)
    assert builds == []
    fallback.retrieve("停機")
    fallback.retrieve("備轉")
    assert builds == [1]
    assert fallback.last_method == "semantic"


def test_factory_rejects_unknown_methods_and_ngram_needs_no_model() -> None:
    common = {
        "ngram_min": 2,
        "ngram_max": 4,
        "lexical_min_score": 0.0,
        "lexical_relative_score": 0.0,
        "embed": BrokenEmbedder(),
        "model": "m",
    }
    assert isinstance(build_retriever_factory("ngram", **common)(EXAMPLES), TfidfRetriever)
    assert isinstance(build_retriever_factory("hybrid", **common)(EXAMPLES), FallbackRetriever)
    with pytest.raises(ValueError):
        build_retriever_factory("bm25", **common)


def test_ollama_embedder_reports_an_unreachable_server_as_embedding_error() -> None:
    with pytest.raises(EmbeddingError):
        OllamaEmbedder("http://127.0.0.1:9", "bge-m3", timeout_seconds=0.5)(["測試"])
    assert OllamaEmbedder("http://127.0.0.1:9", "bge-m3")([]) == []


def test_retrieval_metrics() -> None:
    queries = [{"intent": "a"}, {"intent": "b"}]
    rankings = [["a", "x", "a", "x", "x"], ["x", "x", "x", "x", "x", "b"]]

    metrics = score_rankings(queries, rankings)

    assert metrics == {
        "hit_at_1": 0.5,
        "hit_at_5": 0.5,
        "precision_at_5": 0.2,
        "mrr": round((1 + 1 / 6) / 2, 4),
    }


def test_retrieval_queries_have_labels_and_skip_other() -> None:
    queries = load_queries()

    assert len(queries) >= 100
    assert all(query["intent"] and query["intent"] != "other" for query in queries)
