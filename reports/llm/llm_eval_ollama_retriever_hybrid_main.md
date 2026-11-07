# LLM 路徑評測：ollama（qwen3:8b）・retriever_hybrid_main

> provider 額外參數：`{"reasoning_effort": "none"}`；RAG 範例：開

> 產生時間 2026-11-07T23:22:19+00:00。題庫 `benchmarks/llm_questions.json`：只收離線規則接不住、但資料答得出來的問題；以 gold SQL 的執行結果比對（不看欄名與順序）。

| 指標 | 值 |
|---|---:|
| 計分題數（排除規則接得住的 0 題） | 20 |
| 答對 | 3 |
| 答錯（有回答但結果不符） | 1 |
| 沒有回答（守門擋下、反問或 LLM 失敗） | 16 |
| 準確率 | 15.0% |
| 每題秒數（中位數／最大） | 305.4／307.5 |

| 題目 | 結果 | 秒數 | 嘗試 | 錯誤碼 |
|---|---|---:|---:|---|
| 各縣市各有幾部機組 | no_answer | 306.4 | 1 | LLM_UNAVAILABLE |
| 各燃料別各有幾部機組 | no_answer | 307.5 | 1 | LLM_UNAVAILABLE |
| 每座電廠的機組數，由多到少排列 | no_answer | 305.7 | 1 | MISSING_PARAMETER |
| 各縣市的總裝置容量（瓩） | no_answer | 305.5 | 1 | LLM_UNAVAILABLE |
| 高雄市的機組總裝置容量 | correct | 191.8 | 1 |  |
| 新北市有哪些電廠 | no_answer | 306.1 | 1 | LLM_UNAVAILABLE |
| 商轉日期最早的兩部機組 | no_answer | 305.0 | 1 | LLM_UNAVAILABLE |
| 裝置容量超過80萬瓩的機組有哪些 | no_answer | 305.2 | 1 | LLM_UNAVAILABLE |
| 哪些電廠同時有燃煤和天然氣機組 | no_answer | 305.2 | 1 | MISSING_PARAMETER |
| 尚未有商轉日期精確到日的機組有幾部 | no_answer | 305.4 | 1 | LLM_UNAVAILABLE |
| 歲修天數最長的五筆歲修 | no_answer | 305.2 | 1 | LLM_UNAVAILABLE |
| 2026年7月20日各類別的尖峰出力合計 | wrong_result | 175.5 | 1 |  |
| 2026年7月20日尖峰出力超過50萬瓩的機組欄位有幾個 | no_answer | 306.2 | 1 | MISSING_PARAMETER |
| 台中#1在2026年7月的平均尖峰出力 | no_answer | 305.3 | 1 | LLM_UNAVAILABLE |
| 台中#1在2025年尖峰出力超過50萬瓩的天數 | no_answer | 305.8 | 1 | LLM_UNAVAILABLE |
| 2025年7月和2026年7月的平均尖峰負載各是多少 | no_answer | 305.4 | 1 | MISSING_PARAMETER |
| 2025年備轉容量率的最大值和最小值 | no_answer | 305.7 | 1 | MISSING_PARAMETER |
| 哪一座電廠的機組數最多 | correct | 26.0 | 1 |  |
| 澎湖縣的風力場站2025年總發電量 | correct | 24.8 | 1 |  |
| 風力場站裝置容量排名前五 | no_answer | 305.6 | 1 | MISSING_PARAMETER |
