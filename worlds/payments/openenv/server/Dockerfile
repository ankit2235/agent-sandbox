# The OpenEnv template's Dockerfile (`openenv init`, openenv 0.9.0), with one change: the base
# image ships Python 3.11 and agent-sandbox-bench needs 3.12, so uv installs a 3.12 interpreter
# into /opt/uv-python and the runtime stage copies it beside the venv that links to it.

ARG BASE_IMAGE=ghcr.io/huggingface/openenv-base:latest
FROM ${BASE_IMAGE} AS builder

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

ARG BUILD_MODE=in-repo
ARG ENV_NAME=payments_env

ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

COPY . /app/env

WORKDIR /app/env

RUN if ! command -v uv >/dev/null 2>&1; then \
        curl -LsSf https://astral.sh/uv/install.sh | sh && \
        mv /root/.local/bin/uv /usr/local/bin/uv && \
        mv /root/.local/bin/uvx /usr/local/bin/uvx; \
    fi

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-install-project --no-editable; \
    else \
        uv sync --no-install-project --no-editable; \
    fi

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-editable; \
    else \
        uv sync --no-editable; \
    fi

RUN mv /app/env/.venv /app/.venv

FROM ${BASE_IMAGE}

WORKDIR /app

COPY --from=builder /opt/uv-python /opt/uv-python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/env /app/env

RUN ln -s /app/.venv /app/env/.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/env:$PYTHONPATH"

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["sh", "-c", "cd /app/env && uvicorn server.app:app --host 0.0.0.0 --port 8000"]
