FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.9.2 \
    && useradd --create-home --uid 10001 medbase
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
RUN uv sync --frozen --no-dev && mkdir -p /app/storage && chown medbase:medbase /app/storage
USER medbase
EXPOSE 8000
CMD ["uvicorn", "medbase.api:app", "--host", "0.0.0.0", "--port", "8000"]
