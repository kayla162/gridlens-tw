# GridLens TW 公開展示用映像：只用離線規則，不含本機 LLM，不需要任何 API key。
#
#   docker build -t gridlens-tw .
#   docker run --rm -p 8000:8000 gridlens-tw          # http://127.0.0.1:8000/
#
# Render、Hugging Face Spaces 等平台會用環境變數 PORT 指定埠號；見 docs/DEPLOY.md。

FROM python:3.12.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1 \
    UV_HTTP_TIMEOUT=300

WORKDIR /app

# 先只裝相依套件，原始碼改了也能沿用這一層快取。只裝核心相依：不裝 local（Ollama client）與 dev。
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --no-install-project

COPY . .
RUN uv sync \
    && uv run --no-sync python -m ingest.build_db \
    && useradd --create-home --uid 1000 app \
    && mkdir -p data logs \
    && chown -R app:app data logs reports

USER app

# offline：只用規則，公開環境不把模型開給訪客。管理介面預設只接受 localhost 的 Host，
# 所以部署到公開網址時登入資料管理頁會被拒絕；展示只開放查詢。
ENV POWERQUERY_RUNTIME_MODE=offline \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=4)"

CMD ["sh", "-c", "exec .venv/bin/powerquery --serve --host 0.0.0.0 --port \"$PORT\""]
