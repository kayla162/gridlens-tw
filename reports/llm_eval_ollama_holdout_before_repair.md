# LLM 路徑評測：ollama（qwen3:8b）・holdout_before_repair

> provider 額外參數：`{"reasoning_effort": "none"}`；RAG 範例：開

> 產生時間 2026-10-30T07:35:52+00:00。題庫 `benchmarks/llm_holdout_questions.json`：只收離線規則接不住、但資料答得出來的問題；以 gold SQL 的執行結果比對（不看欄名與順序）。

| 指標 | 值 |
|---|---:|
| 計分題數（排除規則接得住的 0 題） | 20 |
| 答對 | 3 |
| 答錯（有回答但結果不符） | 13 |
| 沒有回答（守門擋下、反問或 LLM 失敗） | 4 |
| 準確率 | 15.0% |
| 每題秒數（中位數／最大） | 19.4／58.8 |

| 題目 | 結果 | 秒數 | 嘗試 | 錯誤碼 |
|---|---|---:|---:|---|
| 各電廠的總裝置容量（萬瓩） | no_answer | 42.6 | 3 | GENERATION_FAILED |
| 金門縣有哪些電廠 | wrong_result | 29.7 | 3 |  |
| 燃料是重油的機組有幾部 | wrong_result | 11.6 | 1 |  |
| 裝置容量最大的三部機組是哪些 | wrong_result | 18.1 | 1 |  |
| 每座電廠最早商轉的日期 | wrong_result | 20.4 | 2 |  |
| 有水力機組的電廠有幾座 | wrong_result | 28.9 | 3 |  |
| 機組超過10部的電廠 | no_answer | 39.1 | 3 | MISSING_PARAMETER |
| 平均每部機組裝置容量最大的電廠 | wrong_result | 16.8 | 1 |  |
| 2000年（含）以後才商轉的機組有幾部 | wrong_result | 14.5 | 1 |  |
| 澎湖縣有幾部機組 | no_answer | 24.9 | 3 | GENERATION_FAILED |
| 南部發電廠在2026年7月1日的尖峰出力合計 | correct | 11.2 | 1 |  |
| 裝置容量最大的風力場站是哪一座 | wrong_result | 21.0 | 1 |  |
| 總裝置容量排名第二的電廠 | wrong_result | 58.8 | 2 |  |
| 有兩種以上燃料的電廠有幾座 | wrong_result | 18.0 | 2 |  |
| 商轉日期只到月份的機組分布在哪些電廠 | correct | 7.7 | 1 |  |
| 協和發電廠和林口發電廠各有幾部機組 | wrong_result | 19.0 | 1 |  |
| 裝置容量在50萬瓩以上、80萬瓩以下的機組有幾部 | correct | 16.2 | 1 |  |
| 每座電廠有幾種燃料 | no_answer | 27.3 | 3 | GENERATION_FAILED |
| 2026年7月20日類別為再生的機組欄位有幾個 | wrong_result | 9.6 | 1 |  |
| 燃氣機組的歲修總共有幾筆 | wrong_result | 19.4 | 2 |  |
