# 開發規範與合併檢查表

每次改動都照這份做；合併門檻檢查（`.claude/skills/pre-merge-check`）裡機器可判定的條目也以文末檢查表為準。
最新的執行與回退證據以 [log.md](../log.md) 最上方的儲存點為準；啟動與 API 契約見 [SERVING](SERVING.md)、
[API 契約](../src/serving/API_CONTRACT.md)。

## 開始前

1. 讀 README、[SERVING](SERVING.md)、[API 契約](../src/serving/API_CONTRACT.md) 與 log.md 最上方的儲存點。
2. `git status` 確認工作目錄；保留原有的未提交變更，不 reset、checkout、刪除或順手提交無關檔案。

## 改動時

3. 不繞過唯讀 SQL、語意守門、人工資料審核與人工語料審核。
4. 不把 API key、密碼、session、真實識別資料、執行期資料庫、學習工作區或大型原始資料加入 Git。
5. 離線 CI 必須可重現；本機 LLM 功能不得讓沒有 Ollama 的核心測試失敗。
6. 高風險改動（資料切換、帳號權限、語料發布、稽核）要加負向、競態或故障注入測試。
7. 行為有變時，README、SERVING 與 API 契約同步更新。

## 完成時

8. 跑相關測試；改到共用介面時跑全套：`pytest`、`ruff format --check`、`ruff check`、
   `node --check src/serving/static/app.js`、`git diff --check HEAD`。
9. 每個要 commit 的可驗證儲存點，commit 前先在 log.md 新增一個 `## CP-xxx` 區塊，
   寫明狀態、驗證證據與「回退方式」；測試沒過不得標示完成。
10. commit 訊息用 Conventional Commits；一個 commit 只放這次的改動。

## 儲存點與合併檢查表

- [ ] `git status` 已確認，沒有覆寫或暫存無關的變更。
- [ ] 相關測試通過；改到共用介面時已跑全套測試。
- [ ] 沒有把憑證、個資、執行期資料庫、學習工作區或大型原始資料加入 Git。
- [ ] README／SERVING／API 契約已在行為變更時同步。
- [ ] `log.md` 已在 commit 前記錄驗證證據與回退方式。
- [ ] commit 只包含這次的改動；需要上傳時已確認 push 成功。
