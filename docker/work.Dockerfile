FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /workspace

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_OFFLINE=1 \
    PYTHONPATH=/workspace/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /opt/dependencies
COPY pyproject.toml uv.lock ./
RUN UV_OFFLINE=0 uv sync --locked --all-groups --no-install-project \
    && UV_OFFLINE=0 uv pip install --python /opt/venv/bin/python hatchling
WORKDIR /workspace
