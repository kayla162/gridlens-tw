"""The model-comparison table is built from per-model reports and paired against the baseline."""

from __future__ import annotations

import json

import pytest

from eval import compare_models
from eval.compare_models import SETS, extra_body_for, label, report_path, summarize


def _write(model: str, question_set: str, outcomes: list[str], seconds: float) -> None:
    results = [
        {"id": f"{question_set}-{index}", "outcome": outcome, "rule_hit": False, "seconds": seconds}
        for index, outcome in enumerate(outcomes)
    ]
    report = {
        "questions_file": f"{question_set}.json",
        "measured": len(outcomes),
        "correct": outcomes.count("correct"),
        "wrong_result": outcomes.count("wrong_result"),
        "no_answer": outcomes.count("no_answer"),
        "vram_gb": 5.1,
        "results": results,
    }
    path = report_path(model, question_set)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")


def test_labels_are_file_safe_and_qwen3_gets_thinking_off() -> None:
    assert label("qwen2.5-coder:7b", "fresh3") == "model-qwen2.5-coder-7b_fresh3"
    assert extra_body_for("qwen3:4b") == {"reasoning_effort": "none"}
    assert extra_body_for("gemma3:4b") == {}


def test_summary_pools_every_set_and_pairs_against_the_first_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compare_models, "REPORTS", tmp_path)
    for question_set in SETS:
        _write("base:8b", question_set, ["correct", "correct", "wrong_result"], 4.0)
        _write("small:4b", question_set, ["correct", "wrong_result", "no_answer"], 2.0)
    out = tmp_path / "table.md"

    text = summarize(["base:8b", "small:4b"], out=out)

    assert out.read_text(encoding="utf-8") == text
    assert "| `base:8b` | **10**/15（67%）" in text
    assert "（基準）" in text
    # 每份題庫第二題：基準對、小模型錯 → 5 題只有基準對，0 題只有小模型對。
    assert "5 / 0，p = 0.062" in text
    assert "| `small:4b` | 1/3 | 1/3 | 1/3 | 1/3 | 1/3 |" in text


def test_summary_refuses_to_build_a_table_with_missing_reports(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compare_models, "REPORTS", tmp_path)
    _write("base:8b", "fresh3", ["correct"], 1.0)

    with pytest.raises(SystemExit, match="缺少報告"):
        summarize(["base:8b"], out=tmp_path / "table.md")
