"""MCP server: let an agent (Claude Desktop, Claude Code, ...) ask the power data in Chinese.

設計重點是**工具只收中文問題，不收 SQL**。代理送來的問題走的是和網頁、CLI 完全同一條管線：
規則路由、語意守門（例如拒答「尖峰出力跨日加總」）、語法樹 SQL 守門、唯讀 SQLite；
長尾才交給本機 LLM。
代理沒有任何繞過守門的路：不能直接下 SQL、不能讀原始開放資料檔、不能碰管理介面或寫入任何東西。

    # stdio（給 Claude Desktop／Claude Code 設定用）
    uv run --extra mcp python -m serving.mcp_server
    uv run --extra mcp python -m serving.mcp_server --mode offline   # 只用規則，不碰 Ollama

身分等同在本機跑 CLI 的人：全電廠範圍、只查受管制的語意檢視。設定方式見 docs/MCP.md。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ingest.validate import PROJECT_ROOT
from serving.coverage import describe_coverage, load_coverage_document
from serving.runtime import ServiceRuntime, build_runtime

MODE_ENV = "POWERQUERY_RUNTIME_MODE"
MODES = ("offline", "local", "auto")
MAX_QUESTION_CHARS = 500  # 和 `/api/query` 的 QueryRequest 一致
MAX_ROWS = 50  # 給模型看的列數上限；完整筆數另外回報
COVERAGE_CONFIG = PROJECT_ROOT / "configs" / "coverage.yaml"
EXAMPLE_QUESTIONS = (
    "資料涵蓋到什麼時候",
    "2026年7月備轉容量率最低是哪一天？",
    "2026年7月20日出力前五名機組",
    "天然氣機組共有幾台？",
    "2026年三月有哪些機組在歲修？",
    "2025年台灣太陽能發了多少度？",
)

INSTRUCTIONS = (
    "GridLens TW：台灣電力公司開放資料（機組、尖峰出力、備轉容量率、歲修、再生能源發電量、"
    "即時發電）的唯讀查詢。\n"
    "用繁體中文的自然語言問題呼叫 query_power_data；不要嘗試傳 SQL，系統會自己產生並檢查。\n"
    "不確定資料有沒有某段期間或某種指標時，先呼叫 describe_data_coverage。\n"
    "被拒答（severity = refuse）代表問題本身在資料上沒有意義，照 suggestions 改問，"
    "不要換句話硬問同一件事。"
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def answer(runtime: ServiceRuntime, question: str, *, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Run one question through the guarded pipeline and shape the result for a model."""

    question = question.strip()
    if not question:
        return _failure("EMPTY_QUESTION", "問題不可為空。")
    if len(question) > MAX_QUESTION_CHARS:
        return _failure("QUESTION_TOO_LONG", f"問題最多 {MAX_QUESTION_CHARS} 個字。")

    response = runtime.pipeline.query(question).to_dict()
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if not response["success"]:
        return {
            "success": False,
            "error_code": response.get("error_code", "ERROR"),
            "error": response.get("error", "查詢失敗"),
            "severity": response.get("severity", "error"),
            "suggestions": list(response.get("suggestions", [])),
        }
    rows = list(data.get("rows", []))
    realtime = data.get("realtime") if isinstance(data.get("realtime"), dict) else None
    return {
        "success": True,
        "question": question,
        "columns": list(data.get("columns", [])),
        "rows": rows[:max_rows],
        "row_count": data.get("record_count", len(rows)),
        "truncated": len(rows) > max_rows,
        "disclosures": [item.get("reason") for item in data.get("disclosures", [])],
        "sql": data.get("sql"),
        "params": list(data.get("params", [])),
        "answered_by": "rules" if data.get("source") == "router" else "local_llm",
        "runtime_mode": runtime.mode,
        # 即時資料每 10 分鐘更新一次；轉述時要說是哪個時段的數字。
        "realtime_data_time": realtime.get("data_time") if realtime else None,
    }


def coverage(runtime: ServiceRuntime, config: Path = COVERAGE_CONFIG) -> dict[str, Any]:
    views, limitations = load_coverage_document(config)
    realtime = getattr(runtime.pipeline, "realtime", None)
    try:
        return describe_coverage(
            runtime.database,
            views=views,
            limitations=limitations,
            realtime=realtime.coverage() if realtime is not None else None,
        )
    except sqlite3.Error:
        return _failure("COVERAGE_UNAVAILABLE", "目前無法讀取資料涵蓋範圍。")


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message, "severity": "error"}


def create_server(runtime_factory: Callable[[], ServiceRuntime]) -> FastMCP:
    """Build the server. The runtime is built on the first tool call, once."""

    lock = threading.Lock()
    cache: list[ServiceRuntime] = []

    def runtime() -> ServiceRuntime:
        with lock:
            if not cache:
                cache.append(runtime_factory())
            return cache[0]

    server = FastMCP("gridlens-tw", instructions=INSTRUCTIONS, log_level="WARNING")

    @server.tool(title="用中文查台電資料", annotations=READ_ONLY)
    def query_power_data(question: str) -> dict[str, Any]:
        """用繁體中文問台電開放資料，回傳查詢結果、產生的 SQL 與資料限制說明。

        例：「2026年7月備轉容量率最低是哪一天？」「2026年7月20日出力前五名機組」。
        只收自然語言問題，不收 SQL。結果最多回 50 列（row_count 是完整筆數）。
        disclosures 是資料對這個答案的限制，轉述答案時要一併說明。
        """

        return answer(runtime(), question)

    @server.tool(title="資料涵蓋範圍", annotations=READ_ONLY)
    def describe_data_coverage() -> dict[str, Any]:
        """列出目前資料庫有哪些資料、各自的期間與筆數，以及這份資料答不出的問題類型。"""

        return coverage(runtime())

    @server.tool(title="範例問題", annotations=READ_ONLY)
    def list_example_questions() -> list[str]:
        """回傳幾個這個系統答得出來的問題，當作問法的參考。"""

        return list(EXAMPLE_QUESTIONS)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GridLens TW 的 MCP server（stdio）")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=os.environ.get(MODE_ENV, "auto").strip().casefold() or "auto",
        help="offline 只用規則；local 長尾交給 Ollama；auto（預設）偵測得到 Ollama 才用",
    )
    parser.add_argument("--database", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.mode not in MODES:
        parser.error(f"{MODE_ENV} 必須是 {'／'.join(MODES)}")
    server = create_server(lambda: build_runtime(mode=args.mode, database=args.database))
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
