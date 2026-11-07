"""How much of an LLM-eval score difference is signal, and how much is the small question set.

每份 LLM 題庫只有 20～28 題，「15 → 20」只差 5 題。這裡提供兩個不需要 scipy 的工具：

- **Wilson 信賴區間**：單一份報告的準確率，題數少時比常態近似可靠（不會超出 0～1）。
- **McNemar 精確檢定**：同一份題庫、兩次執行（修正前後、兩個模型）逐題配對，
  只看「一邊對、一邊錯」的題目，用二項分布算雙尾 p 值。兩次都對或都錯的題目不提供差異的資訊。
- **配對 bootstrap**：逐題重抽，給出準確率差的信賴區間，和 McNemar 互相對照。

    uv run python -m eval.stats reports/llm_eval_ollama_fresh3_before.json \\
        reports/llm_eval_ollama_fresh3_filters.json

第一份是基準，後面每一份都和它配對比較；`--markdown` 另外輸出表格。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Z_95 = 1.959963984540054


def wilson_interval(correct: int, total: int, *, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for ``correct / total``."""

    if total <= 0:
        raise ValueError("total must be positive")
    if not 0 <= correct <= total:
        raise ValueError("correct must be between 0 and total")
    p = correct / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(only_baseline: int, only_candidate: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts.

    ``only_baseline``：基準答對、候選答錯的題數；``only_candidate``：反過來。
    虛無假設下每一題不一致的方向各 1/2，所以是 n = b + c、p = 0.5 的二項檢定。
    """

    if only_baseline < 0 or only_candidate < 0:
        raise ValueError("counts must be non-negative")
    n = only_baseline + only_candidate
    if n == 0:
        return 1.0
    k = min(only_baseline, only_candidate)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_bootstrap(
    baseline: Sequence[bool],
    candidate: Sequence[bool],
    *,
    iterations: int = 10_000,
    seed: int = 0,
) -> tuple[float, float]:
    """95% percentile interval of ``accuracy(candidate) - accuracy(baseline)``, resampling items."""

    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("need two equally long, non-empty outcome lists")
    rng = random.Random(seed)
    n = len(baseline)
    deltas = [int(c) - int(b) for b, c in zip(baseline, candidate, strict=True)]
    samples = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(iterations))
    low = samples[int(0.025 * iterations)]
    high = samples[min(iterations - 1, int(0.975 * iterations))]
    return low, high


@dataclass(frozen=True)
class Comparison:
    baseline: str
    candidate: str
    items: int
    baseline_correct: int
    candidate_correct: int
    only_baseline: int
    only_candidate: int
    p_value: float
    delta_low: float
    delta_high: float

    @property
    def delta(self) -> float:
        return (self.candidate_correct - self.baseline_correct) / self.items


def outcomes(report: dict[str, Any]) -> dict[str, bool]:
    """Scored questions only: rule hits never reached the LLM and are not part of the score."""

    return {
        result["id"]: result["outcome"] == "correct"
        for result in report["results"]
        if not result.get("rule_hit")
    }


def compare(
    baseline: dict[str, Any], candidate: dict[str, Any], *, names: tuple[str, str] = ("A", "B")
) -> Comparison:
    left, right = outcomes(baseline), outcomes(candidate)
    shared = sorted(set(left) & set(right))
    if not shared:
        raise ValueError("the two reports share no scored question")
    b = [left[key] for key in shared]
    c = [right[key] for key in shared]
    only_baseline = sum(1 for x, y in zip(b, c, strict=True) if x and not y)
    only_candidate = sum(1 for x, y in zip(b, c, strict=True) if y and not x)
    low, high = paired_bootstrap(b, c)
    return Comparison(
        baseline=names[0],
        candidate=names[1],
        items=len(shared),
        baseline_correct=sum(b),
        candidate_correct=sum(c),
        only_baseline=only_baseline,
        only_candidate=only_candidate,
        p_value=mcnemar_exact(only_baseline, only_candidate),
        delta_low=low,
        delta_high=high,
    )


def accuracy_row(name: str, report: dict[str, Any]) -> str:
    scored = outcomes(report)
    correct, total = sum(scored.values()), len(scored)
    low, high = wilson_interval(correct, total)
    return f"| {name} | {correct}/{total} | {correct / total:.0%} | {low:.0%}～{high:.0%} |"


def comparison_row(comparison: Comparison) -> str:
    return (
        f"| {comparison.baseline} → {comparison.candidate} | {comparison.items} | "
        f"{comparison.baseline_correct} → {comparison.candidate_correct} | "
        f"{comparison.only_baseline} / {comparison.only_candidate} | "
        f"{comparison.delta:+.0%}（{comparison.delta_low:+.0%}～{comparison.delta_high:+.0%}） | "
        f"{comparison.p_value:.3f} |"
    )


ACCURACY_HEADER = ["| 報告 | 答對 | 準確率 | 95% Wilson 區間 |", "|---|---:|---:|---|"]
COMPARISON_HEADER = [
    "| 比較 | 配對題數 | 答對 | 只有前者對／只有後者對 | 準確率差（95% bootstrap） | McNemar p |",
    "|---|---:|---:|---:|---|---:|",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 評測報告的信賴區間與配對檢定")
    parser.add_argument("reports", nargs="+", type=Path, help="第一份是基準，其餘和它配對比較")
    parser.add_argument("--markdown", type=Path, default=None, help="另外把表格寫成 markdown")
    args = parser.parse_args(argv)
    loaded = [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in args.reports]
    lines = [*ACCURACY_HEADER, *(accuracy_row(name, report) for name, report in loaded)]
    if len(loaded) > 1:
        base_name, base = loaded[0]
        lines += ["", *COMPARISON_HEADER]
        lines += [
            comparison_row(compare(base, report, names=(base_name, name)))
            for name, report in loaded[1:]
        ]
    text = "\n".join(lines)
    print(text)
    if args.markdown:
        args.markdown.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
