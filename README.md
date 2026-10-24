# agent-sandbox

An open-source, [OpenEnv](https://github.com/huggingface/OpenEnv)-compatible sandbox platform for
measuring how well AI agents do real work inside simulated **worlds**. One shared, domain-neutral
kit; each world is one domain built on top of it.

An agent receives a task, acts **only** through that world's tools, and is scored by deterministic
graders that read the world's final state and the trace of what the agent did.

```bash
just setup          # uv sync: Python 3.12 + the workspace
just test           # the whole suite
just run-example    # one scripted episode in the hello-world world, graded
just worlds         # what is installed
```

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
| `payments` | Phase 1–3 | Pays through simulated UPI-inspired rails, buys from unreliable merchants, chases refunds and disputes. |
| `security` | Phase 3S | Works an alert queue as a SOC analyst: triage, investigate, contain, escalate, report. Defensive only. |
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

## Layout

```
kit/          core types, episode loop, trace, clock, rng, world registry, OpenEnv adapter
worlds/       one directory per domain; nothing outside worlds/ imports them
stress/       generic, seeded modifiers that make any task harder
runner/       the `sandbox` CLI: agent loop, runs, traces, scoring, export, verify
docs/         DECISIONS.md, PROGRESS.md, and the docs site
```

## Status

Phases 0–5 of the build plan: the core kit, the payments and security worlds with 50 tasks each,
the runner that scores models on them and exports leaderboard files, and the stress library and
hidden-set tooling. No grader is signed off
by a domain reviewer yet, so both worlds are listed as `preview`. The interfaces are tagged
`kit-v0.1` once the pair leads have reviewed them; see [docs/PROGRESS.md](docs/PROGRESS.md) for
what exists today and [docs/DECISIONS.md](docs/DECISIONS.md) for why.

## License

Apache-2.0. See [LICENSE](LICENSE).
