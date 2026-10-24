# The agent-sandbox image: the `sandbox` command with every world and LiteLLM, installed from
# uv.lock. Published to ghcr.io/prarabdha-soni/agent-sandbox by .github/workflows/release.yml.
#
#   docker build -t agent-sandbox .
#   docker run --rm agent-sandbox quickstart --model fake:golden
#   docker run --rm -e OPENAI_API_KEY -v "$PWD/runs:/work/runs" agent-sandbox \
#       run --world payments --tasks payments-v0.1 --model openai/gpt-4.1-mini --out runs/x

FROM ghcr.io/astral-sh/uv:0.12.20-python3.12-trixie-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages --extra litellm --no-editable

FROM python:3.12-slim-trixie
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /work
ENTRYPOINT ["sandbox"]
CMD ["--help"]
