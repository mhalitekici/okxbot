FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --create-home bot \
    && mkdir -p /app/data \
    && chown bot:bot /app/data
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
ENV PATH="/app/.venv/bin:$PATH" TELEGRAM_SESSION_FILE=/app/data/telegram.session-string
USER bot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4)"
CMD ["python", "scripts/start.py"]

