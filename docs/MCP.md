# MCP server：讓 AI 代理用中文查台電資料

`serving.mcp_server` 用 [Model Context Protocol](https://modelcontextprotocol.io) 把 GridLens TW 開給
Claude Desktop、Claude Code 或其他支援 MCP 的代理。代理在對話中遇到電力資料的問題時，自己呼叫工具查，
再把結果和資料限制轉述給使用者。

## 設計：工具只收問題，不收 SQL

最簡單的做法是開一個 `run_sql` 工具，但那等於把守門交給代理自己決定。這裡反過來：

| 層 | 代理能做什麼 |
|---|---|
| 工具輸入 | 只有一個 `question` 字串（最多 500 字）；沒有 SQL、資料表或模式參數 |
| 管線 | 和網頁、CLI 同一條：規則路由 → 語意守門 → 語法樹 SQL 守門 → 唯讀 SQLite；長尾才交給本機 LLM |
| 拒答 | 「尖峰出力跨日加總」這類在資料上沒意義的問題回 `severity: refuse` 加改問建議，server 說明裡要求代理別換句話硬問 |
| 範圍 | 只查受管制的語意檢視；不能讀原始開放資料檔、不能碰管理介面、沒有任何寫入 |
| 工具標註 | 三個工具都標 `readOnlyHint: true`、`destructiveHint: false`、`openWorldHint: false` |

身分等同在本機跑 `powerquery` CLI 的人（全電廠範圍）。MCP server 只走 stdio、不開網路埠，
所以只有啟動它的那個程式連得到。

## 工具

| 工具 | 用途 |
|---|---|
| `query_power_data(question)` | 回 `columns`、`rows`（最多 50 列，`row_count` 是完整筆數、`truncated` 標示有沒有截斷）、`sql`、`params`、`disclosures`（資料限制）、`answered_by`（`rules` 或 `local_llm`）；失敗時回 `error_code`、`error`、`severity`、`suggestions` |
| `describe_data_coverage()` | 各資料檢視的期間與筆數、這份資料答不出的問題類型（和 `/api/coverage` 同一份） |
| `list_example_questions()` | 幾個答得出來的問法 |

## 設定

先 `uv sync --extra mcp --extra local`（`local` 是本機 LLM 用的 client；只用規則可省略）並建好資料庫（`make db`）。

**Claude Desktop**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "gridlens-tw": {
      "command": "uv",
      "args": [
        "run", "--directory", "C:/path/to/gridlens-tw",
        "--extra", "mcp", "--extra", "local",
        "python", "-m", "serving.mcp_server"
      ]
    }
  }
}
```

**Claude Code**：

```bash
claude mcp add gridlens-tw -- uv run --directory /path/to/gridlens-tw --extra mcp --extra local python -m serving.mcp_server
```

模式跟著 `POWERQUERY_RUNTIME_MODE`，或在參數最後加 `--mode offline|local|auto`（預設 `auto`：偵測得到 Ollama 才用）。
`--mode offline` 只用規則，不碰 Ollama，回應都在一秒內。

## 驗證

- `tests/test_mcp_server.py` 用 SDK 的 in-memory transport 走真的 MCP 協定（initialize → list_tools → call_tool），
  CI 不需要 Ollama：工具清單與唯讀標註、查詢結果、語意守門拒答、SQL 不能繞過管線、列數上限、runtime 延遲建立、
  範例問題全部答得出來。
- 實際以 stdio 子行程連線（和 Claude Desktop 一樣的啟動方式），`auto` 模式、Ollama 開著：
  - 「2026年7月20日出力前五名機組」→ 規則回答，含啟動 8.8 秒；
  - 「台中一號機去年尖峰出力總和」→ 語意守門拒答 `PEAK_SUM_ACROSS_DAYS`；
  - 「哪些電廠同時有燃煤和天然氣機組」→ 交給本機 LLM，23 秒。這題 qwen3:8b 把「同時有」寫成 `OR`，
    回的是「有燃煤或天然氣」的電廠 —— 守門擋得住危險與無意義的 SQL，擋不住語意寫錯的 SQL，
    所以回應裡附上 `sql` 讓代理與使用者看得到它查了什麼。
