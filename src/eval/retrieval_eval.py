"""Offline retrieval metrics: character n-gram vs local embeddings vs hybrid.

檢索的工作是把「同一類問題」的範例放進 prompt。所以相關的定義是：語料範例的 `intent`
與問句的人工標註 `intent` 相同。問句來自 `golden_questions.json`（80 題）與
`eval_questions.json`（60 題），兩份都不在語料裡（`test_no_leakage` 會檢查）；`other` 是雜項，不計。

指標（每題取前 5 名）：
- Hit@1：第一名就是同一類。
- Hit@5：前五名至少有一個同一類 —— 模型至少看得到一個對的範例。
- Precision@5：前五名有幾成是同一類 —— 也就是 retriever.yaml 說的「純度」，
  其餘是會把模型帶偏的雜訊。
- MRR：第一個同類範例名次的倒數平均。

    uv run python -m eval.retrieval_eval                 # 需要本機 Ollama 與 bge-m3
    uv run python -m eval.retrieval_eval --model bge-m3 --out reports/retrieval_eval
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingest.validate import PROJECT_ROOT
from text2sql.corpus import load_corpus
from text2sql.embedding_retriever import EmbeddingRetriever, HybridRetriever, OllamaEmbedder
from text2sql.retriever import TfidfRetriever

QUESTION_FILES = ("golden_questions.json", "eval_questions.json")
TOP_K = 5


def load_queries(root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name in QUESTION_FILES:
        for item in json.loads((root / "benchmarks" / name).read_text(encoding="utf-8")):
            if item.get("intent") and item["intent"] != "other":
                items.append(
                    {"id": item["id"], "question": item["question"], "intent": item["intent"]}
                )
    return items


def score_rankings(
    queries: Sequence[dict[str, Any]], rankings: Sequence[Sequence[str]]
) -> dict[str, float]:
    """``rankings[i]`` is the ordered list of retrieved example intents for ``queries[i]``."""

    hit1 = hit5 = precision = reciprocal = 0.0
    for query, intents in zip(queries, rankings, strict=True):
        top = list(intents[:TOP_K])
        relevant = [intent == query["intent"] for intent in intents]
        hit1 += bool(top) and top[0] == query["intent"]
        hit5 += any(relevant[:TOP_K])
        precision += sum(relevant[:TOP_K]) / TOP_K
        first = next((rank for rank, ok in enumerate(relevant, start=1) if ok), None)
        reciprocal += 1 / first if first else 0.0
    n = len(queries)
    return {
        "hit_at_1": round(hit1 / n, 4),
        "hit_at_5": round(hit5 / n, 4),
        "precision_at_5": round(precision / n, 4),
        "mrr": round(reciprocal / n, 4),
    }


def evaluate(retrievers: dict[str, Any], queries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, retriever in retrievers.items():
        total = len(retriever.examples)
        rankings = [
            [item.example["intent"] for item in retriever.retrieve(query["question"], top_k=total)]
            for query in queries
        ]
        results[name] = score_rankings(queries, rankings)
        print(f"{name:10s} {results[name]}", flush=True)
    return results


def write_report(report: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 檢索評測：字元 n-gram vs 本機 embedding",
        "",
        f"> 產生時間 {report['generated_at']}。"
        f"問句 {report['queries']} 題（golden＋eval，去掉 other），"
        f"語料範例 {report['examples']} 個；相關＝範例的 intent 與問句相同。"
        f"embedding 模型：`{report['model']}`。",
        "",
        "| 檢索方式 | Hit@1 | Hit@5 | Precision@5 | MRR |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metrics in report["results"].items():
        lines.append(
            f"| {name} | {metrics['hit_at_1']:.1%} | {metrics['hit_at_5']:.1%} | "
            f"{metrics['precision_at_5']:.1%} | {metrics['mrr']:.3f} |"
        )
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="字元 n-gram、embedding 與混合檢索的離線指標")
    parser.add_argument("--model", default="bge-m3")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports" / "retrieval_eval")
    args = parser.parse_args(argv)

    examples = load_corpus(PROJECT_ROOT / "corpus" / "training_corpus.json")["examples"]
    queries = load_queries()
    lexical = TfidfRetriever(examples)
    semantic = EmbeddingRetriever(
        examples, OllamaEmbedder(args.base_url, args.model), model=args.model
    )
    results = evaluate(
        {
            "ngram": lexical,
            "embedding": semantic,
            "hybrid": HybridRetriever(lexical, semantic),
        },
        queries,
    )
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": args.model,
        "queries": len(queries),
        "examples": len(examples),
        "top_k": TOP_K,
        "results": results,
    }
    write_report(report, args.out)
    print(f"報告：{args.out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
