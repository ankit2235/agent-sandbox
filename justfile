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
    uv run mypy --strict kit/src/kit guard/src/guard
    uv run mypy runner/src/runner stress/src/stress training/src/training
    uv run mypy -p worlds.payments
    uv run mypy -p worlds.security

# The determinism and domain-neutrality rules, as their own fast check.
invariants:
    uv run pytest kit/tests/test_purity.py kit/tests/test_core_isolation.py

# The hidden-task rule: no canary, no task marked hidden, no published hidden task in the repo.
no-hidden:
    git ls-files -z | xargs -0 python3 scripts/check_no_hidden.py
    uv run sandbox hidden leaks kit runner stress guard training worlds scripts docs

# The guard-model dataset's leakage check, as CI runs it: a small build, then the check.
training-leaks:
    uv run sandbox-train dataset build --config ci --out training/data/out --workers 4
    uv run sandbox-train dataset check training/data/out/actions-v0.1-ci

# Run the hello-world example end to end.
run-example *ARGS:
    uv run sandbox example {{ARGS}}

# List the installed worlds.
worlds:
    uv run sandbox worlds

# Validate every packaged task file.
validate-tasks:
    uv run sandbox tasks validate

# Ten tasks with one model, and a score (no key needed for the default fake model).
quickstart MODEL="fake:golden":
    uv run sandbox quickstart --model {{MODEL}}

# The release gate, e.g. `just release-check payments-v0.1 --results ~/runs/payments-v0.1`.
release-check SET *ARGS:
    uv run sandbox release-check {{SET}} {{ARGS}}

# Build the image episodes run in with `--isolation container` (never pulled, never pushed).
image:
    docker build -t agent-sandbox:0.1.0 .

# The container tests: probes of the locked-down settings, and every task in a container.
test-containers: image
    SANDBOX_TEST_IMAGE=agent-sandbox:0.1.0 uv run pytest -m docker runner/tests/test_isolation_docker.py

# Known vulnerabilities in the Python lock and the TypeScript SDK, as CI checks them.
audit:
    python3 scripts/dependency_scan.py python
    python3 scripts/dependency_scan.py npm guard/typescript

# Build the six distributions into dist/. Nothing is uploaded.
build:
    uv build --all-packages --out-dir dist

# Build the docs site into site/, strictly (a broken link fails).
docs:
    uv run --group docs mkdocs build --strict

# Serve the docs site at http://127.0.0.1:8000 while editing it.
docs-serve:
    uv run --group docs mkdocs serve

# Regenerate the docs site's tool reference from the worlds' tool schemas.
tool-docs:
    for world in payments security; do uv run sandbox tools --world $world > docs/site/tools/$world.md; done

# What CI runs.
ci: lint typecheck test
