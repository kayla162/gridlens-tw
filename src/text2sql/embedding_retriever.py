"""Semantic retrieval with a local embedding model, and a hybrid with the n-gram retriever.

字元 n-gram 只懂字面：「水力電廠有多少座」和「有幾座水力發電廠」共用的字不多，分數就低；
反過來字面相似的範例常把模型帶偏（見 docs/LLM_EVALUATION.md）。這裡用本機 Ollama 的 embedding
模型（預設 `bge-m3`，支援中文）把問句轉成向量比 cosine，資料一樣不離開這台機器。

- `OllamaEmbedder`：呼叫 Ollama 的 `/api/embed`，只用標準函式庫。
- `EmbeddingRetriever`：語料範例的向量建一次（可寫入快取檔），每次查詢只嵌入問句。
- `HybridRetriever`：n-gram 與 embedding 的排名用 Reciprocal Rank Fusion 合併，兩邊的分數尺度不同，
  只看名次不看分數。

三者的 `retrieve()` 介面與 `TfidfRetriever` 相同，pipeline 不必知道用的是哪一種。
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from text2sql.retriever import RetrievedExample, TfidfRetriever

Embed = Callable[[Sequence[str]], list[list[float]]]


class Retriever(Protocol):
    def retrieve(
        self,
        question: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        relative_score: float = 0.0,
    ) -> list[RetrievedExample]: ...


class EmbeddingError(RuntimeError):
    """The embedding model could not be reached or returned something unusable."""


class OllamaEmbedder:
    """Batch embeddings from a local Ollama server (``POST /api/embed``)."""

    def __init__(self, base_url: str, model: str, *, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as error:
            raise EmbeddingError(
                f"無法從 {self.base_url} 取得 {self.model} 的向量：{error}"
            ) from error
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError(f"{self.model} 回傳的向量數量不符")
        return [[float(value) for value in vector] for vector in vectors]


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        raise EmbeddingError("embedding 是零向量")
    return [value / norm for value in vector]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def corpus_fingerprint(examples: Sequence[dict[str, Any]], model: str) -> str:
    """Changes with the model or any example question, so a stale cache is never used."""

    digest = hashlib.sha256(model.encode("utf-8"))
    for example in examples:
        digest.update(b"\0" + str(example["id"]).encode("utf-8"))
        digest.update(b"\0" + example["question"].encode("utf-8"))
    return digest.hexdigest()


class EmbeddingRetriever:
    def __init__(
        self,
        examples: list[dict[str, Any]],
        embed: Embed,
        *,
        model: str = "unknown",
        cache_path: Path | None = None,
    ) -> None:
        self.examples = examples
        self.embed = embed
        self.model = model
        self.vectors = self._corpus_vectors(cache_path)

    def _corpus_vectors(self, cache_path: Path | None) -> list[list[float]]:
        if not self.examples:
            return []
        fingerprint = corpus_fingerprint(self.examples, self.model)
        if cache_path is not None and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}
            if cached.get("fingerprint") == fingerprint:
                return [list(map(float, vector)) for vector in cached["vectors"]]
        vectors = [
            _normalise(vector) for vector in self.embed([e["question"] for e in self.examples])
        ]
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"model": self.model, "fingerprint": fingerprint, "vectors": vectors}
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return vectors

    def scores(self, question: str) -> list[float]:
        (query,) = self.embed([question])
        query = _normalise(query)
        return [_dot(query, vector) for vector in self.vectors]

    def retrieve(
        self,
        question: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        relative_score: float = 0.0,
    ) -> list[RetrievedExample]:
        if not self.examples or top_k <= 0:
            return []
        ranked = sorted(
            (
                RetrievedExample(score, example)
                for score, example in zip(self.scores(question), self.examples, strict=True)
            ),
            key=lambda item: (-item.score, item.example["id"]),
        )[:top_k]
        floor = max(min_score, ranked[0].score * relative_score)
        return [item for item in ranked if item.score >= floor]


class FallbackRetriever:
    """Use the semantic retriever, but never let a down Ollama break a query.

    語料向量在第一次查詢時才建（服務啟動不必等 Ollama）；建不起來或查詢時連不上，就改用字元 n-gram，
    並記下原因。`last_method` 讓 trace 看得出這一題實際用哪一種檢索。
    """

    def __init__(
        self,
        lexical: TfidfRetriever,
        build_semantic: Callable[[], Retriever],
        *,
        lexical_min_score: float = 0.0,
        lexical_relative_score: float = 0.0,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        # pipeline 傳進來的門檻是 embedding 的 cosine 尺度；退回 n-gram 時改用 n-gram 自己的門檻。
        self.lexical = lexical
        self.lexical_min_score = lexical_min_score
        self.lexical_relative_score = lexical_relative_score
        self.examples = lexical.examples
        self._build_semantic = build_semantic
        self._semantic: Retriever | None = None
        self._logger = logger
        self.last_method = "ngram"
        self.last_error: str | None = None

    def retrieve(
        self,
        question: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        relative_score: float = 0.0,
    ) -> list[RetrievedExample]:
        try:
            if self._semantic is None:
                self._semantic = self._build_semantic()
            results = self._semantic.retrieve(
                question, top_k=top_k, min_score=min_score, relative_score=relative_score
            )
        except EmbeddingError as error:
            self.last_method, self.last_error = "ngram", str(error)
            if self._logger:
                self._logger(f"embedding 檢索失敗，改用字元 n-gram：{error}")
            return self.lexical.retrieve(
                question,
                top_k=top_k,
                min_score=self.lexical_min_score,
                relative_score=self.lexical_relative_score,
            )
        self.last_method, self.last_error = "semantic", None
        return results


class HybridRetriever:
    """Reciprocal Rank Fusion of n-gram and embedding rankings: score = Σ 1 / (k + rank)."""

    def __init__(
        self,
        lexical: TfidfRetriever,
        semantic: EmbeddingRetriever,
        *,
        rrf_k: int = 60,
    ) -> None:
        if lexical.examples is not semantic.examples and [e["id"] for e in lexical.examples] != [
            e["id"] for e in semantic.examples
        ]:
            raise ValueError("兩個檢索器必須用同一份語料")
        self.lexical = lexical
        self.semantic = semantic
        self.examples = semantic.examples
        self.rrf_k = rrf_k

    def retrieve(
        self,
        question: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        relative_score: float = 0.0,
    ) -> list[RetrievedExample]:
        """門檻套在 embedding 的 cosine 上：RRF 分數只有名次資訊，沒辦法當門檻。"""

        if not self.examples or top_k <= 0:
            return []
        total = len(self.examples)
        lexical = self.lexical.retrieve(question, top_k=total)
        semantic = self.semantic.retrieve(question, top_k=total)
        cosine = {item.example["id"]: item.score for item in semantic}
        fused: dict[str, float] = {}
        by_id = {example["id"]: example for example in self.examples}
        for ranking in (lexical, semantic):
            for rank, item in enumerate(ranking, start=1):
                key = item.example["id"]
                fused[key] = fused.get(key, 0.0) + 1.0 / (self.rrf_k + rank)
        ranked = sorted(fused, key=lambda key: (-fused[key], key))[:top_k]
        floor = max(min_score, cosine[ranked[0]] * relative_score) if ranked else 0.0
        return [RetrievedExample(fused[key], by_id[key]) for key in ranked if cosine[key] >= floor]


RETRIEVAL_METHODS = ("ngram", "embedding", "hybrid")


def build_retriever_factory(
    method: str,
    *,
    ngram_min: int,
    ngram_max: int,
    lexical_min_score: float,
    lexical_relative_score: float,
    embed: Embed,
    model: str,
    cache_path: Path | None = None,
    logger: Callable[[str], None] | None = None,
) -> Callable[[list[dict[str, Any]]], Any]:
    """Factory the pipeline calls with the corpus examples (again after every corpus reload)."""

    if method not in RETRIEVAL_METHODS:
        raise ValueError(f"檢索方式必須是 {', '.join(RETRIEVAL_METHODS)} 之一，收到 {method!r}")

    def lexical(examples: list[dict[str, Any]]) -> TfidfRetriever:
        return TfidfRetriever(examples, minimum=ngram_min, maximum=ngram_max)

    if method == "ngram":
        return lexical

    def factory(examples: list[dict[str, Any]]) -> FallbackRetriever:
        words = lexical(examples)

        def semantic() -> Retriever:
            vectors = EmbeddingRetriever(examples, embed, model=model, cache_path=cache_path)
            return vectors if method == "embedding" else HybridRetriever(words, vectors)

        return FallbackRetriever(
            words,
            semantic,
            lexical_min_score=lexical_min_score,
            lexical_relative_score=lexical_relative_score,
            logger=logger,
        )

    return factory
