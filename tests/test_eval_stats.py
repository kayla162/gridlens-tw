"""Confidence intervals and paired tests for LLM-eval reports."""

from __future__ import annotations

import json
import math

import pytest

from eval.stats import compare, main, mcnemar_exact, paired_bootstrap, wilson_interval


def _report(outcomes: dict[str, str], *, rule_hits: tuple[str, ...] = ()) -> dict:
    return {
        "results": [
            {"id": key, "outcome": value, "rule_hit": key in rule_hits}
            for key, value in outcomes.items()
        ]
    }


def test_wilson_interval_matches_the_textbook_value_and_stays_inside_zero_one() -> None:
    low, high = wilson_interval(20, 28)
    assert math.isclose(low, 0.5291, abs_tol=1e-3)
    assert math.isclose(high, 0.8484, abs_tol=1e-3)
    assert wilson_interval(0, 10)[0] == 0.0
    assert math.isclose(wilson_interval(10, 10)[1], 1.0)
    with pytest.raises(ValueError):
        wilson_interval(3, 0)
    with pytest.raises(ValueError):
        wilson_interval(5, 4)


def test_mcnemar_exact_is_a_two_sided_binomial_on_discordant_items() -> None:
    assert mcnemar_exact(0, 0) == 1.0
    assert math.isclose(mcnemar_exact(0, 5), 2 / 32)
    assert math.isclose(mcnemar_exact(5, 0), mcnemar_exact(0, 5))
    assert math.isclose(mcnemar_exact(1, 6), 2 * 8 / 128)
    assert mcnemar_exact(3, 3) == 1.0
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 2)


def test_paired_bootstrap_is_reproducible_and_brackets_the_observed_delta() -> None:
    baseline = [True] * 10 + [False] * 10
    candidate = [True] * 15 + [False] * 5
    first = paired_bootstrap(baseline, candidate, iterations=2000)
    assert first == paired_bootstrap(baseline, candidate, iterations=2000)
    assert first[0] <= 0.25 <= first[1]
    assert paired_bootstrap(baseline, baseline, iterations=500) == (0.0, 0.0)
    with pytest.raises(ValueError):
        paired_bootstrap([True], [True, False])


def test_compare_pairs_by_question_id_and_skips_rule_hits() -> None:
    before = _report({"a": "correct", "b": "wrong_result", "c": "no_answer", "d": "correct"})
    after = _report(
        {"d": "correct", "c": "correct", "b": "correct", "a": "wrong_result", "x": "correct"},
        rule_hits=("x",),
    )

    result = compare(before, after, names=("before", "after"))

    assert result.items == 4
    assert (result.baseline_correct, result.candidate_correct) == (2, 3)
    assert (result.only_baseline, result.only_candidate) == (1, 2)
    assert math.isclose(result.delta, 0.25)
    assert result.p_value == 1.0


def test_compare_refuses_reports_without_shared_questions() -> None:
    with pytest.raises(ValueError):
        compare(_report({"a": "correct"}), _report({"b": "correct"}))


def test_cli_writes_a_markdown_table(tmp_path, capsys) -> None:
    left = tmp_path / "before.json"
    right = tmp_path / "after.json"
    left.write_text(json.dumps(_report({"a": "wrong_result", "b": "correct"})), encoding="utf-8")
    right.write_text(json.dumps(_report({"a": "correct", "b": "correct"})), encoding="utf-8")
    out = tmp_path / "table.md"

    assert main([str(left), str(right), "--markdown", str(out)]) == 0

    text = out.read_text(encoding="utf-8")
    assert "| before | 1/2 |" in text
    assert "| before → after | 2 | 1 → 2 | 0 / 1 |" in text
    assert text.strip() == capsys.readouterr().out.strip()
