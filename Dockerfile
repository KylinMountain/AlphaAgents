# AlphaAgents — trading-day scheduler + optional web UI
FROM python:3.12-slim

# curl: healthcheck. tzdata: Asia/Shanghai scheduling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer — rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY alpha_agents/ ./alpha_agents/
COPY main.py ./
RUN uv sync --frozen --no-dev

# data/ holds SQLite DBs and the Chroma index; always bind-mount it.
VOLUME ["/app/data"]

# Trading-day scheduler (morning scan / intraday / review / weekly).
CMD ["python", "main.py", "run-v2"]
