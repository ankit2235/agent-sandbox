# The agent-sandbox image: the `sandbox` command with every world and LiteLLM, installed from
# uv.lock. Published to ghcr.io/prarabdha-soni/agent-sandbox by .github/workflows/release.yml,
# and the image `sandbox run --isolation container` starts each episode in (docs/site/isolation.md).
#
#   docker build -t agent-sandbox:0.1.0 .
#   docker run --rm agent-sandbox:0.1.0 quickstart --model fake:golden
#   docker run --rm --user "$(id -u):$(id -g)" -e OPENAI_API_KEY -v "$PWD/runs:/work/runs" \
#       agent-sandbox:0.1.0 run --world payments --tasks payments-v0.1 \
#       --model openai/gpt-4.1-mini --out runs/x
#
# Base images are pinned by digest (the tag is kept for readers); update both together.

FROM ghcr.io/astral-sh/uv:0.12.20-python3.12-trixie-slim@sha256:ca99e1db564ab49c1b93e39007d7c1f39e9184c4b26b15c9f826095dfe59f40b AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages --extra litellm --no-editable

FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
COPY --from=build /app/.venv /app/.venv
# The bytecode is compiled at build time; nothing needs to write it at run time, and an
# episode's root filesystem is read-only.
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
# A non-root default user. `sandbox run --isolation container` passes its own --user.
RUN mkdir /work && chown 65532:65532 /work
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["sandbox"]
CMD ["--help"]
