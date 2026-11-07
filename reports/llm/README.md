# 本機 LLM 評測報告

`python -m eval.llm_eval` 的輸出，每次執行一份 `.json`（逐題結果）與一份 `.md`（摘要）。
檔名是 `llm_eval_<provider>_<label>`。完整的實驗過程與解讀見 [docs/LLM_EVALUATION.md](../../docs/LLM_EVALUATION.md)。

## 先看這幾份（現況）

| 題庫 | 報告 | 答對 |
|---|---|---:|
| fresh3（28 題，第三輪的成績） | [llm_eval_ollama_fresh3_filters](llm_eval_ollama_fresh3_filters.md)、[重跑](llm_eval_ollama_fresh3_filters_run2.md) | 20 |
| fresh2（26 題） | [llm_eval_ollama_fresh2_filters](llm_eval_ollama_fresh2_filters.md) | 22 |
| fresh（27 題） | [llm_eval_ollama_fresh_filters](llm_eval_ollama_fresh_filters.md) | 26 |
| holdout（20 題） | [llm_eval_ollama_holdout_filters](llm_eval_ollama_holdout_filters.md) | 17 |
| main（20 題） | [llm_eval_ollama_filters](llm_eval_ollama_filters.md) | 18 |
| 推理開（fresh3／main） | [fresh3](llm_eval_ollama-reasoning_fresh3_reasoning.md)、[main](llm_eval_ollama-reasoning_reasoning_repaired.md) | 25／14 |

多模型比較的報告以 `llm_eval_model-*` 開頭，彙整表在 [model_comparison.md](model_comparison.md)。

## 其餘是過程紀錄

`*_before*` 是各輪修正前的基準，`*_gaps`、`*_shape`、`*_repair*` 等是中間步驟，`*_run2` 是重跑。
它們保留下來，是為了讓 [log.md](../../log.md) 每個儲存點引用的數字都查得到；配對檢定也要用到修正前的逐題結果：

```bash
uv run python -m eval.stats reports/llm/llm_eval_ollama_fresh3_before.json reports/llm/llm_eval_ollama_fresh3_filters.json
```
