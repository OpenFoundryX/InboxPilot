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

# ffmpeg/ffprobe: transcoding uploaded media down to speech-sized audio before
# transcription, and reading its true duration for metering. The worker needs
# them; they are in the shared base stage because the API image is built from
# the same target. Without this layer the transcode works on a developer's Mac
# (Homebrew) and fails only in the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies (the project itself is virtual — package = false).
COPY pyproject.toml ./
COPY uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# Copy the source. src/ is on PYTHONPATH so modules import as top-level.
COPY . .

EXPOSE 8000

# Default command runs the API; compose overrides this for worker/beat.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# Test stage — adds pytest/ruff/mypy on top of `base`'s already-synced
# production deps. api/worker/beat all pin `target: base` in docker-compose.yml
# and never see dev tooling; only the compose `test` service targets this
# stage, and only `make test` uses that service.
FROM base AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra dev
CMD ["pytest"]
