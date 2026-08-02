# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# uv: fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

WORKDIR /app

# Install dependencies (the project itself is virtual — package = false).
COPY pyproject.toml ./
COPY uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra dev

# Copy the source. src/ is on PYTHONPATH so modules import as top-level.
COPY . .

EXPOSE 8000

# Default command runs the API; compose overrides this for worker/beat.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
