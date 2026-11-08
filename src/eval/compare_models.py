"""Compare local models on every LLM question set, with the same pipeline, guards and repairs.

面試常問「為什麼用 qwen3:8b？」。這裡把幾個能在 6 GB 顯示卡上跑的本機模型放進**同一條管線**
（同樣的檢索、守門、SQL 修正與答案形狀修正），用五份題庫一起量，並和基準模型逐題配對檢定。

注意偏差：五份題庫的錯誤都是照 qwen3:8b 的輸出修的，SQL 修正與答案形狀修正也是為它的錯誤寫的，
所以這個比較**偏向 qwen3:8b**；其他模型若接近，代表它們實際上可能更好。

    # 跑（需要 Ollama 與各模型；每個模型約 10～20 分鐘）
    uv run --extra local python -m eval.compare_models run qwen3:8b qwen3:4b qwen2.5-coder:7b
    # 只用已經跑好的報告重算彙整表
    uv run python -m eval.compare_models summarize qwen3:8b qwen3:4b qwen2.5-coder:7b
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.stats import compare, wilson_interval
from ingest.validate import PROJECT_ROOT

REPORTS = PROJECT_ROOT / "reports" / "llm"
SETS = ("fresh3", "fresh2", "fresh", "holdout", "main")
# qwen3 系列預設會先「思考」；和正式設定一樣關掉推理。其他模型不認這個參數，給空的 extra_body。
THINKING_OFF = {"reasoning_effort": "none"}


def safe_name(model: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "." else "-" for ch in model)


def label(model: str, question_set: str) -> str:
    return f"model-{safe_name(model)}_{question_set}"


def report_path(model: str, question_set: str, provider: str = "ollama") -> Path:
    return REPORTS / f"llm_eval_{provider}_{label(model, question_set)}.json"


def extra_body_for(model: str) -> dict[str, Any]:
    return dict(THINKING_OFF) if model.startswith("qwen3") else {}


def loaded_size_gb(model: str, base_url: str = "http://127.0.0.1:11434") -> float | None:
    """Memory Ollama reports for the model right after the run (``/api/ps``), in GB."""

    try:
        with urllib.request.urlopen(f"{base_url}/api/ps", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    for item in payload.get("models", []):
        if item.get("name") == model or item.get("model") == model:
            return round(item.get("size_vram", item.get("size", 0)) / 1e9, 1)
    return None


def run(models: Sequence[str], *, timeout: float, database: Path) -> None:
    from eval.llm_eval import QUESTION_SETS, evaluate, write_reports

    for model in models:
        for question_set in SETS:
            report = evaluate(
                "ollama",
                database=database,
                model=model,
                timeout_seconds=timeout,
                extra_body=extra_body_for(model),
                label=label(model, question_set),
                questions=QUESTION_SETS[question_set],
            )
            report["vram_gb"] = loaded_size_gb(model)
            write_reports(report, REPORTS)
            print(f"{model} {question_set}: {report['correct']}/{report['measured']}", flush=True)


def _load(model: str, question_set: str) -> dict[str, Any] | None:
    path = report_path(model, question_set)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _pooled(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Concatenate scored results of several reports, prefixing ids with the question file."""

    results = [
        {**result, "id": f"{report['questions_file']}:{result['id']}"}
        for report in reports
        for result in report["results"]
    ]
    return {"results": results}


def summarize(models: Sequence[str], *, out: Path) -> str:
    baseline_model = models[0]
    rows: list[str] = []
    per_set: list[str] = []
    for model in models:
        reports = {s: _load(model, s) for s in SETS}
        missing = [s for s, report in reports.items() if report is None]
        if missing:
            raise SystemExit(f"{model} 缺少報告：{', '.join(missing)}；先跑 `run`")
        present = [reports[s] for s in SETS]
        correct = sum(r["correct"] for r in present)
        wrong = sum(r["wrong_result"] for r in present)
        unanswered = sum(r["no_answer"] for r in present)
        total = sum(r["measured"] for r in present)
        low, high = wilson_interval(correct, total)
        seconds = [
            result["seconds"]
            for r in present
            for result in r["results"]
            if not result.get("rule_hit")
        ]
        vram = next((r.get("vram_gb") for r in present if r.get("vram_gb")), None)
        if model == baseline_model:
            versus = "（基準）"
        else:
            comparison = compare(
                _pooled([_load(baseline_model, s) for s in SETS]),
                _pooled(present),
                names=(baseline_model, model),
            )
            p_text = "< 0.001" if comparison.p_value < 0.001 else f"= {comparison.p_value:.3f}"
            versus = f"{comparison.only_baseline} / {comparison.only_candidate}，p {p_text}"
        rows.append(
            f"| `{model}` | **{correct}**/{total}（{correct / total:.0%}） | "
            f"{low:.0%}～{high:.0%} | "
            f"{wrong} | {unanswered} | {statistics.median(seconds):.1f} | "
            f"{vram if vram is not None else '—'} | {versus} |"
        )
        cells = " | ".join(f"{reports[s]['correct']}/{reports[s]['measured']}" for s in SETS)
        per_set.append(f"| `{model}` | {cells} |")
    lines = [
        "# 本機模型比較",
        "",
        f"> 產生時間 {datetime.now(UTC).isoformat(timespec='seconds')}。"
        "五份題庫（規則接不住、資料答得出來的問題）合計；"
        "同一條管線、同樣的守門與修正，推理關、temperature 0。",
        f"> 配對檢定以 `{baseline_model}` 為基準："
        "「只有基準對／只有該模型對」的題數與 McNemar 精確檢定 p 值。",
        "> **偏差**：題庫的錯誤與修正程式都是照 qwen3:8b 調的，比較偏向它。",
        "",
        "| 模型 | 答對 | 95% Wilson | 答錯 | 沒有回答 | 每題秒數（中位數） "
        "| 顯示卡記憶體 GB | 對基準 |",
        "|---|---:|---|---:|---:|---:|---:|---|",
        *rows,
        "",
        "| 模型 | " + " | ".join(SETS) + " |",
        "|---|" + "---:|" * len(SETS),
        *per_set,
    ]
    text = "\n".join(lines) + "\n"
    out.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在同一條管線上比較多個本機模型")
    parser.add_argument("action", choices=["run", "summarize"])
    parser.add_argument("models", nargs="+", help="第一個是配對檢定的基準")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data/processed/power.db")
    parser.add_argument("--out", type=Path, default=REPORTS / "model_comparison.md")
    args = parser.parse_args(argv)
    if args.action == "run":
        run(args.models, timeout=args.timeout, database=args.database)
    print(summarize(args.models, out=args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
