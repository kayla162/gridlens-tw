"""The MCP server speaks the real protocol and routes every question through the same guards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from ingest.build_db import build_database  # noqa: E402
from serving.mcp_server import EXAMPLE_QUESTIONS, answer, create_server  # noqa: E402
from serving.runtime import ServiceRuntime, build_runtime  # noqa: E402


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> ServiceRuntime:
    path = tmp_path_factory.mktemp("mcp") / "power.db"
    build_database(path)
    return build_runtime(database=path, mode="offline")


def _call(server, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    async def go():
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            result = await client.call_tool(tool, arguments or {})
            return listed, result

    return anyio.run(go)


def _payload(result) -> Any:
    assert not result.isError, result.content
    if result.structuredContent is not None:
        content = result.structuredContent
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(result.content[0].text)


def test_tools_are_listed_as_read_only_and_take_a_question_not_sql(runtime) -> None:
    listed, _ = _call(create_server(lambda: runtime), "list_example_questions")

    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == {"query_power_data", "describe_data_coverage", "list_example_questions"}
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
    assert list(tools["query_power_data"].inputSchema["properties"]) == ["question"]


def test_query_returns_rows_sql_and_who_answered(runtime) -> None:
    _, result = _call(
        create_server(lambda: runtime),
        "query_power_data",
        {"question": "2026年7月備轉容量率最低是哪一天？"},
    )

    data = _payload(result)
    assert data["success"] is True
    assert data["rows"] == [["2026-07-05", 10.23]]
    assert data["columns"] == ["日期", "備轉容量率_pct"]
    assert data["sql"].startswith("SELECT")
    assert data["answered_by"] == "rules"
    assert data["runtime_mode"] == "offline"


def test_semantic_guard_refusal_reaches_the_agent_with_suggestions(runtime) -> None:
    data = answer(runtime, "台中一號機去年尖峰出力總和")

    assert data["success"] is False
    assert data["error_code"] == "PEAK_SUM_ACROSS_DAYS"
    assert data["severity"] == "refuse"
    assert data["suggestions"]


def test_sql_is_not_a_way_around_the_pipeline(runtime) -> None:
    data = answer(runtime, "DROP TABLE units; SELECT * FROM sqlite_master")

    assert data["success"] is False
    assert "rows" not in data


def test_empty_and_oversized_questions_are_rejected_before_the_pipeline() -> None:
    def explode() -> ServiceRuntime:
        raise AssertionError("不該建 runtime")

    class NoPipeline:
        pipeline = property(lambda self: explode())

    assert answer(NoPipeline(), "   ")["error_code"] == "EMPTY_QUESTION"  # type: ignore[arg-type]
    assert answer(NoPipeline(), "電" * 501)["error_code"] == "QUESTION_TOO_LONG"  # type: ignore[arg-type]


def test_rows_are_capped_but_the_full_count_is_reported(runtime) -> None:
    data = answer(runtime, "列出台中發電廠所有設備", max_rows=3)

    assert data["success"] is True
    assert len(data["rows"]) == 3
    assert data["row_count"] > 3
    assert data["truncated"] is True


def test_runtime_is_built_lazily_once(runtime) -> None:
    builds: list[int] = []

    def factory() -> ServiceRuntime:
        builds.append(1)
        return runtime

    server = create_server(factory)
    assert builds == []
    _call(server, "list_example_questions")
    assert builds == []
    _call(server, "describe_data_coverage")
    _call(server, "query_power_data", {"question": "天然氣機組共有幾台？"})
    assert builds == [1]


def test_coverage_and_examples(runtime) -> None:
    server = create_server(lambda: runtime)

    coverage = _payload(_call(server, "describe_data_coverage")[1])
    examples = _payload(_call(server, "list_example_questions")[1])

    assert "success" not in coverage or coverage["success"] is not False
    assert examples == list(EXAMPLE_QUESTIONS)
    for question in EXAMPLE_QUESTIONS:
        assert answer(runtime, question)["success"] is True, question


def test_main_rejects_an_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from serving import mcp_server

    monkeypatch.setenv(mcp_server.MODE_ENV, "cloud")
    with pytest.raises(SystemExit):
        mcp_server.main([])


def test_server_module_documents_how_to_run() -> None:
    doc = Path(__file__).resolve().parents[1] / "docs" / "MCP.md"
    assert "serving.mcp_server" in doc.read_text(encoding="utf-8")
