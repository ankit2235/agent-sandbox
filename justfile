# Agent sandbox — common tasks. Run `just` for the list.

default:
    @just --list

# Install the workspace (creates .venv with Python 3.12) and the dev tools.
setup:
    uv sync --all-packages

# Install the optional OpenEnv extra as well (heavy; needed for the adapter tests).
setup-openenv:
    uv sync --all-packages --extra openenv

# Install LiteLLM, for running real models (heavy; CI only uses the fake models).
setup-models:
    uv sync --all-packages --extra litellm

# Run every test.
test *ARGS:
    uv run pytest {{ARGS}}

# Lint and check formatting.
lint:
    uv run ruff check .
    uv run ruff format --check .

# Fix what can be fixed automatically.
format:
    uv run ruff check --fix .
    uv run ruff format .

# Strict type checking on the kit; the rest is checked non-strictly.
typecheck:
    uv run mypy --strict kit/src/kit
    uv run mypy runner/src/runner stress/src/stress
    uv run mypy -p worlds.payments
    uv run mypy -p worlds.security

# The determinism and domain-neutrality rules, as their own fast check.
invariants:
    uv run pytest kit/tests/test_purity.py kit/tests/test_core_isolation.py

# The hidden-task rule: no canary, no task marked hidden, no published hidden task in the repo.
no-hidden:
    git ls-files -z | xargs -0 python3 scripts/check_no_hidden.py
    uv run sandbox hidden leaks kit runner stress worlds scripts docs

# Run the hello-world example end to end.
run-example *ARGS:
    uv run sandbox example {{ARGS}}

# List the installed worlds.
worlds:
    uv run sandbox worlds

# Validate every packaged task file.
validate-tasks:
    uv run sandbox tasks validate

# What CI runs.
ci: lint typecheck test
