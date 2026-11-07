# 部署公開展示

公開展示只跑**離線規則**：不含本機 LLM、不需要 API key、資料庫在建置映像時從 repo 裡的 `taipower_align/` 重建。
它和 [PUBLIC_OFFLINE_SERVING.md](../PUBLIC_OFFLINE_SERVING.md)（從自己的 Windows 電腦經 Tailscale 對外）是兩條獨立的路。

## 本機用 Docker 跑

```bash
docker build -t gridlens-tw .
docker run --rm -p 8000:8000 gridlens-tw
```

開 <http://127.0.0.1:8000/>。健康檢查：`curl http://127.0.0.1:8000/api/health`，回應中的 `"active_mode":"offline"` 代表只用規則。

映像約 330 MB（`python:3.12-slim` 加上 FastAPI、sqlglot 與 SQLite 資料庫），以非 root 使用者執行，內建 `HEALTHCHECK`。

## 公開環境的安全預設

| 項目 | 映像裡的設定 | 結果 |
|---|---|---|
| 執行模式 | `POWERQUERY_RUNTIME_MODE=offline` | 只用規則；映像裡也沒有 Ollama client |
| 管理介面 | 不設 `POWERQUERY_ADMIN_ALLOWED_HOSTS` | 預設只接受 `localhost` 的 Host，公開網址登入一律 403，訪客只能查詢 |
| 帳號名冊、`.env`、執行期資料庫 | `.dockerignore` 排除 | 不會被打包進映像 |
| 即時發電 | 不跑收集器 | 面板顯示「沒有即時資料」，即時問句會說明原因 |

要在公開環境開放資料管理，必須自己設強密碼的帳號名冊（`POWERQUERY_ACCOUNT_ROSTER`）與 `POWERQUERY_ADMIN_ALLOWED_HOSTS`，
平台在反向代理後面時再設 `POWERQUERY_TRUSTED_PROXIES`；細節見 [SERVING](SERVING.md)。展示用途不建議打開。

## Render（免費方案）

repo 根目錄的 [`render.yaml`](../render.yaml) 是 Render Blueprint：

1. 在 [Render](https://render.com) 選 **New → Blueprint**，連到這個 GitHub repo。
2. 它會用 `Dockerfile` 建一個 `gridlens-tw` web service，健康檢查打 `/api/health`。Render 用 `PORT` 指定埠號，映像會照著聽。
3. 免費方案閒置約 15 分鐘會休眠，下一次開啟要等約一分鐘喚醒。放在履歷上的連結，面試前先自己點開一次。

`autoDeploy: false`：push 到 main 不會自動重新部署，要在 Render 的控制台手動部署，避免還沒驗證的改動直接上線。

## Hugging Face Spaces（Docker）

1. 建一個新的 Space，SDK 選 **Docker**。
2. Space repo 的 `README.md` 開頭加上：

   ```yaml
   ---
   title: GridLens TW
   sdk: docker
   app_port: 7860
   ---
   ```

3. 把這個專案推到 Space repo，並在 Space 的 **Settings → Variables** 加上 `PORT=7860`。

## 重錄 README 的展示動畫

```bash
docker run --rm -d -p 8091:8000 --name gridlens-demo gridlens-tw
uv run --with playwright --with pillow python scripts/record_demo.py --url http://127.0.0.1:8091/
docker stop gridlens-demo
```

腳本用本機的 Edge 開無頭瀏覽器，依序問三題（一般查詢、資料涵蓋範圍的揭露、惡意 SQL），截圖後組成 `docs/media/demo.gif`。
