# agent-sandbox

An open-source, [OpenEnv](https://github.com/huggingface/OpenEnv)-compatible sandbox platform for
measuring how well AI agents do real work inside simulated **worlds**. One shared, domain-neutral
kit; each world is one domain built on top of it.

An agent receives a task, acts **only** through that world's tools, and is scored by deterministic
graders that read the world's final state and the trace of what the agent did.

```bash
pip install agent-sandbox-bench                # once 0.1.0 is published (pending)
sandbox quickstart --model fake:golden         # ten payments tasks, a score, no key needed
```

From a checkout:

```bash
just setup          # uv sync: Python 3.12 + the workspace
just test           # the whole suite
just quickstart     # ten payments tasks with the fake model, and a score
just worlds         # what is installed
```

Docs: <https://prarabdha-soni.github.io/agent-sandbox/>, once GitHub Pages is enabled (sources in
`docs/site/`; `just docs` builds them). The packages are published as **`agent-sandbox-bench`**: the name `agent-sandbox` on
PyPI belongs to an unrelated project.

## What this is

- A **research and evaluation** tool. Worlds are simulations: fake rails, fake merchants, fake
  logs, fake reports.
- **Deterministic.** Seeded randomness, a simulated clock, no internet inside a world. The same
  agent actions produce the same score, always.
- **Graded by plain Python** over final state plus trace. An LLM judge is used only where nothing
  else works, and is flagged when it is.
- **Domain-neutral at the core.** `kit/`, `runner/`, `stress/` (later `guard/`, `trust/`, the
  leaderboard) never import a world. Worlds register themselves through the
  `agent_sandbox.worlds` entry point, and CI fails if that line is crossed.

## What this is not

- Not a product, not a business, and not connected to any real system. No real money, no real
  customer data, no real networks, no real security tooling, ever.
- Not a source of medical, financial or legal advice. The drug-safety world, when it arrives, is a
  research benchmark and is **not validated for real pharmacovigilance**.
- Not a place for offensive security material. The security world is defensive only: no attack
  tooling, no exploit code, no real indicators.
- Not a leaderboard you should read as a product ranking. Scores come with confidence intervals and
  a list of what the benchmark does *not* test.

## Worlds

| World | Status | What the agent does |
| --- | --- | --- |
| `hello_world` | shipped (example) | Counts to a target. Exists to prove the kit's interfaces. |
| `payments` | `payments-v0.1`, release candidate (preview) | Pays through simulated UPI-inspired rails, buys from unreliable merchants, chases refunds and disputes. |
| `security` | `security-v0.1`, preview | Works an alert queue as a SOC analyst: triage, investigate, contain, escalate, report. Defensive only. |
| `drug_safety` | Phase 3P, gated | Processes synthetic adverse-event reports: validity, duplicates, coding, seriousness, deadlines. Starts only once a pharmacovigilance physician has committed as reviewer. |

## Running a model

`sandbox run` drives a reference tool-calling agent over a world's MCP tools, grades every run
and writes full traces. CI only ever uses the fake models, so it spends nothing.

```bash
# No API key, no cost: replay every task's golden run through the whole runner.
uv run sandbox run --world payments --model fake:golden --tasks payments-v0.1 --out runs/fake/

# A real model through LiteLLM (any provider or a local model).
uv sync --all-packages --extra litellm
export OPENAI_API_KEY=...            # whatever your provider needs; see the LiteLLM docs
uv run sandbox run --world payments --model gpt-4.1-mini --provider openai --tasks payments-v0.1 \
    --runs 3 --concurrency 4 --max-cost 0.50 --out runs/2026-10-23-gpt-4.1-mini/

# Development budget mode: ten seeded tasks, one run each, a local model.
uv run sandbox run --world security --model ollama/llama3.2 --tasks security-v0.1 \
    --sample 10 --out runs/dev/
```

- **Resumable.** Run the same command again: finished runs are skipped, a run that was killed
  starts again from a fresh world, and a run whose provider kept failing is retried. Changing the
  settings for an existing `--out` is refused.
- **Caps per run**: the task's step cap (or a lower `--max-steps`), `--max-tokens`, and
  `--max-cost` in US dollars. A run that hits a cap stops and is graded as it stands. Provider
  errors are retried with backoff (`--retries`, default 5).
- **What is recorded**: `manifest.json`, `runs/<run_id>/trace.jsonl` (every agent message, tool,
  arguments, response, state change and simulated time) and `result.json` (grade, tokens, cost,
  steps, stop reason), plus OpenTelemetry spans in `spans.jsonl`. Set
  `OTEL_EXPORTER_OTLP_ENDPOINT`, or `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
  `LANGFUSE_HOST`, to send spans on as well (`uv sync --all-packages --extra otlp`).
- **Stressed tasks**: `--tasks payments-v0.1-stressed` runs every public task that a stress
  modifier applies to, one modifier each ([docs/STRESS.md](docs/STRESS.md)).
  `sandbox stress calibrate` measures each modifier's score drop over 2–3 models.
- **Hidden tasks**: `--tasks payments-v0.1-hidden` reads `$HIDDEN_TASKS_DIR/payments/v0.1/`, and
  only if it matches the hashes published in the repository
  ([docs/HIDDEN_SETS.md](docs/HIDDEN_SETS.md)). Hidden results are only ever published as
  aggregates.

```bash
uv run sandbox verify --run runs/2026-10-23-gpt-4.1-mini/     # replay actions, same grades?
uv run sandbox export runs/2026-10-23-gpt-4.1-mini/ runs/another-model/ \
    --out leaderboard/public/data --updated 2026-10-23           # the leaderboard's data files
```

The export writes `worlds.json` and, per world, `results.json`, `tasks.json` and
`traces/<run_id>.json`, each validated against the JSON Schemas in `runner/src/runner/schemas/`.
Scores: `overall` is the mean over tasks of each task's pass rate, with a 95% bootstrap interval
over tasks; `task_success_rate` is the share of tasks passed in every run; `safe_action_rate`
is the share of runs with no unsafe mistake (in payments a money mistake, in security a
disruption of a healthy asset or destroyed evidence).

## Releasing and submitting results

- `sandbox release-check payments-v0.1 --results <run dirs outside git>` is the release gate: nine
  named conditions, each saying what it waits on (a reviewer, real model runs, the hidden set).
  The owner's steps are in [docs/RELEASING.md](docs/RELEASING.md).
- Results are submitted as `submissions/<set>/<model>.json`, naming an archive of the run
  directory; CI fetches it and `sandbox submission check` replays every run
  ([docs/site/submit-results.md](docs/site/submit-results.md)).
- The payments world is also packaged as an OpenEnv / Hugging Face Space environment in
  `worlds/payments/openenv/`, and as a Docker image (`Dockerfile`).

## The guard-model dataset and baselines (Phase 7)

`training/` builds a dataset of labelled high-impact agent actions from the worlds' runs and
scores decision backends (allow, block or ask the user) on it. Nothing in CI calls a model:

```bash
uv run sandbox-train dataset build --config ci --out training/data/out --workers 4
uv run sandbox-train dataset check training/data/out/actions-v0.1-ci       # what CI checks
uv run sandbox-train backends                                               # the packaged configs
uv run sandbox-train eval --backend rules --data training/data/out/actions-v0.1-ci --out rules.json
# A judge needs its model id at run time (and the litellm extra and a key):
uv run sandbox-train eval --backend judge-a --set model=<provider>/<model> --data ... --out judge-a.json
```

The full build, decontaminated against the hidden set, needs `HIDDEN_TASKS_DIR`
(`--config full --decontaminate`). Training runs on a GPU with its own requirements:
[training/src/training/guard_model/README.md](training/src/training/guard_model/README.md).
The dataset card is [training/data/actions-v0.1/DATASET_CARD.md](training/data/actions-v0.1/DATASET_CARD.md);
the evaluation report, pending real runs, is [docs/blog/guard-model.md](docs/blog/guard-model.md).

## Layout

```
kit/          core types, episode loop, trace, clock, rng, world registry, OpenEnv adapter
worlds/       one directory per domain; nothing outside worlds/ imports them
stress/       generic, seeded modifiers that make any task harder
runner/       the `sandbox` CLI: agent loop, runs, traces, scoring, export, verify
guard/        the decision interface every guard backend speaks (the guard library is Phase 8)
training/     the `sandbox-train` CLI: action dataset, baselines, guard-model training, evaluation
docs/         DECISIONS.md, PROGRESS.md, RELEASING.md, the docs site (site/) and the write-up (blog/)
submissions/  results submissions: one small file per model, never the run directories
```

## Status

Phases 0–7 of the build plan: the core kit, the payments and security worlds with 50 tasks each,
the runner, the stress library and hidden sets, the v0.1 release tooling (packages, Docker
image, docs site, release gate, results submission, OpenEnv packaging), and the guard-model
dataset, baselines, training configs and evaluation (no model trained or run yet). The release gate does not
pass yet: no grader is signed off by a domain reviewer, so both worlds are listed as `preview`,
and no real model has been run. See [docs/PROGRESS.md](docs/PROGRESS.md) for what exists and what
waits on whom, and [docs/DECISIONS.md](docs/DECISIONS.md) for why.

## License

Apache-2.0. See [LICENSE](LICENSE).
