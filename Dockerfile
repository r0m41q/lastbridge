FROM python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS base

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv /app/venv \
    && /app/venv/bin/python -m pip install --no-cache-dir uv==0.13.6

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --inexact \
    && /app/venv/bin/python -m pip uninstall -y uv pip

FROM base AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/venv/bin:$PATH"

WORKDIR /app

RUN python -m pip uninstall -y pip
COPY --from=builder /app/venv /app/venv

RUN useradd --uid 10001 --create-home lastbridge \
    && mkdir -p /app/data/cache /app/data/saved /app/credentials \
    && chown -R lastbridge:lastbridge /app/data /app/credentials

USER lastbridge
ENV LASTBRIDGE_HOST=0.0.0.0 LASTBRIDGE_DATA_DIR=/app/data
EXPOSE 5274
CMD ["lastbridge"]
