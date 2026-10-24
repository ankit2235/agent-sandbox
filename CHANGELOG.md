# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the release plan:
`v0.1` payments, `v0.2` security plus guard, `v0.3` trust layer plus drug safety.

## [Unreleased]

This section becomes **[0.1.0] — payments-v0.1** when the repo owner tags `payments-v0.1` and
`v0.1.0` (`docs/RELEASING.md`). The release gate does not pass yet: it waits on the payments
reviewer's sign-off, real model results and the real calibration report.

### Added — Phase 6: v0.1 release

- Packaging: six distributions at 0.1.0, pinned to each other, published as
  `agent-sandbox-bench` (`agent-sandbox` on PyPI is an unrelated project), with
  `scripts/rename_distribution.py` to change the name in one go. The umbrella sdist holds only
  its metadata; a test builds every archive and checks what is inside.
- `sandbox quickstart --model <id>`: ten seeded tasks, one run each, and a score.
- `sandbox release-check <set>`: nine named conditions (public tasks, hidden set, sign-off,
  grader runs, frozen content, calibration, results for 5+ real models reproduced by
  `verify`, published leaderboard files equal to those results, no hidden content), each saying
  what it waits on. `sandbox freeze <set>` and `tasks/frozen-v0.1.json` for payments.
- Results submission: `submissions/<set>/<model>.json` (`submission-v1`), `sandbox submission
  fetch | check | spot-check`, a pull request template and `.github/workflows/results-submission.yml`
  (a secret-free check on every results pull request, and a maintainer-started spot-check that
  reruns ten seeded tasks with the model).
- `sandbox tools --world <name>`: the tool reference, generated from what the agent is served.
- The docs site (`mkdocs.yml`, `docs/site/`, `mkdocs-material==9.7.7` in a `docs` group) and
  `.github/workflows/docs.yml` (strict build on pull requests, GitHub Pages from main).
- `Dockerfile` (the `sandbox` command with every world and LiteLLM) and
  `.github/workflows/release.yml` (PyPI trusted publishing and the GHCR image on a version tag).
  A `package` job in CI installs the built wheels into a clean virtualenv and runs the quickstart.
- `worlds/payments/openenv/`: the payments world as an OpenEnv / Hugging Face Space
  environment, with a `finish_episode` action; it passes `openenv validate`.
- `docs/RELEASING.md` (every step that needs the repo owner), `docs/blog/v0.1.md` (method, setup
  and limitations; findings pending real runs), `docs/calibration/`.

### Changed — Phase 6

- Distribution names: `agent-sandbox-*` → `agent-sandbox-bench-*`. Imports and the `sandbox`
  command are unchanged.
- `task_hash` leaves out `reviewer_signoff`, so a run made before a sign-off still verifies.
- Kit (additive, on the `kit-v0.1` review list): the OpenEnv adapter's reset observation lists
  the task's tools (`tools`).
- `runner/validate.py` holds the task-file checks and the run player that `tasks validate`,
  `hidden check` and the gate share.

### Added — Phase 5: stress library and hidden set

- `stress/core`: the five generic modifier contracts, shared seeded wording, the `Modifier` and
  `WorldStress` types, `apply(task, [modifiers], seed)` with aliases, `stressed_set`,
  `per_modifier`, and `scripted_runs` (a stressed task's golden and trap runs from its base
  task's). Worlds register their modifiers through the `agent_sandbox.stress` entry point.
- Payments stress modifiers: `misleading_content`, `conflicting_instructions`, `pressure`,
  `flaky_service` (alias `flaky_rails`), `shifted_constraint` (alias `limit_shift`),
  `fake_offer` and `lookalike_payee`, with their checks in `initial_state.stress_checks`.
- Security: its five modifiers registered with runs, the alias `conflicting_instructions`, and
  the guards `left_stress_noise_alone`, `verdicts_held` and `escalated_in_time`.
- Task sets `<world>-<version>-stressed` (33 payments, 50 security tasks).
- `sandbox stress list | calibrate | report`: the calibration report (`report.md`, `report.json`)
  with the score drop per modifier and model, and a verdict per modifier.
- `sandbox hidden generate | hashes | check | scan | leaks`: the hidden set in a private
  repository (`HIDDEN_TASKS_DIR`), one seeded canary per task, a golden and a trap run per task,
  a published sha256 list per world (`tasks/hidden-<version>.json`), and a canary search over
  model output.
- `.github/workflows/no-hidden-leak.yml`: no canary, no task marked hidden and no copy of a
  published hidden task in the public repository, checked in CI.
- `docs/STRESS.md` and `docs/HIDDEN_SETS.md` (with the rotation policy).

### Changed — Phase 5

- Payments `grade()` runs a stressed task's checks first, and reports the first failed one as
  the failure type.
- The runner loads a hidden set only from `HIDDEN_TASKS_DIR` and only if it matches its
  published hashes and carries valid canaries; a task marked hidden is refused from any other
  directory. `fake:golden` and `fake:wrong` replay stressed and hidden tasks' runs.
- `scripts/check_no_hidden.py` looks for a full canary rather than its prefix.

### Added — Phase 4: runner, model adapters, tracing, scoring

- `sandbox run --world <w> --model <id> --tasks <set> --runs N --concurrency N --out <dir>`: a
  reference tool-calling agent over the world's MCP tools (the kit's bridge, a real MCP client
  session), with fixed, versioned system prompts per world (`runner/prompts/`). Resumable (it
  skips finished runs and redoes killed or errored ones), retries provider errors with backoff,
  and enforces step, token and dollar caps per run (`--max-steps`, `--max-tokens`, `--max-cost`).
  `--sample N` runs a seeded subset. Records tokens, cost, steps and stop reason per run.
- Model adapters (`runner/models.py`): any LiteLLM model id (extra `litellm`, `litellm==1.104.2`),
  and fake models for CI: `fake:golden`, `fake:wrong` and `fake:idle` replay a task's run files.
- Traces as JSONL per run, plus OpenTelemetry spans (`spans.jsonl`; OTLP or Langfuse by
  environment variable, extra `otlp`).
- Scoring (`runner/score.py`): per-task pass rate; per model `overall` and `task_success_rate`
  with 95% bootstrap intervals over tasks, `safe_action_rate`, `avg_cost_usd`, `avg_steps`,
  `by_category` and failure counts. Hidden runs are scored only in aggregate.
- `sandbox export <run-dir>... --out <dir> --updated YYYY-MM-DD`: `worlds.json` and each world's
  `results.json`, `tasks.json` (public tasks only) and `traces/<run_id>.json`, in the
  leaderboard's format (`results-v0.1`), each validated against a JSON Schema in
  `runner/schemas/` before it is written.
- `sandbox verify --run <dir>`: replays every recorded run's actions on a fresh world and
  confirms the same world steps and the same grade.
- Kit: `kit.scripted` now holds the YAML run player (`RunFile`, `Run`, `Step`, `load_runs`,
  `resolve_refs`, `play`); `WorldSpec.listing` (`Listing`, `MetricDef`) and
  `WorldSpec.runs_dir`; `CheckResult.failure_type`; `BaseEpisode.router`.
- Leaderboard listings for `payments` and `security` (both `preview`).

### Changed — Phase 4

- Both worlds' `graders/runs.py` are thin wrappers over the kit's player, and both worlds'
  `grade()` record the failure type of every failed check. Their episodes no longer define
  `router` themselves.
- `uv.lock`: adding litellm moved `openai`, `huggingface-hub` and `filelock` to older majors (see
  decision 114).

### Added — Phase 3S: security operations world (defensive only)

- `worlds/security`, the second world, on the kit with no kit changes: five FastAPI + SQLite
  services (`org`, `telemetry`, `alerts`, `response`, `tickets`), each a kit `WorldState`.
- `scenarios/`: the story format with ground truth (`format.py`), the default company
  (`orgs/default.yaml`), seeded background telemetry (`noise.py`), and 21 synthetic stories
  (11 malicious, 10 benign look-alikes). Only RFC 5737 addresses and RFC 2606 domains load.
- 17 MCP tools (`tools-security-v0.1`, `tools/schemas.json`, not frozen): the prompt's sixteen
  plus `collect_evidence`. Served by the kit's MCP bridge (`python -m worlds.security.tools.server`).
- Failure types `security.missed_compromise`, `security.false_containment`,
  `security.evidence_destroyed`, `security.late_escalation` (high impact),
  `security.false_positive_escalated`, `security.over_containment`, `security.wrong_root_cause`,
  `security.incomplete_report`.
- 50 public tasks, task set `security-v0.1` (12 triage, 10 investigation, 10 containment,
  8 reporting, 10 multi; 15 easy, 20 medium, 15 hard), with `tasks/schema.json`. No task carries a
  reviewer sign-off yet.
- Graders (`graders/common.py` plus one per task) with guards that earn no credit on their own and
  an impact penalty for disrupting healthy assets; 147 golden and wrong runs in `graders/runs/`.
- Five stress modifiers in `worlds/security/stress/`: `alert_flood`, `decoy_anomaly`,
  `misleading_note`, `partial_telemetry`, `time_pressure`.
- A security reward-hacking review in `docs/DECISIONS.md` (decisions 86–106), with tests.
- CI runs `uv run mypy -p worlds.security` and `worlds/security/tests`.

### Added — Phase 3: tasks and graders (payments)

- 50 public payments tasks, task set `payments-v0.1`, in
  `worlds/payments/src/worlds/payments/tasks/<category>/`: 15 rails, 15 commerce, 12 disputes,
  8 multi; 15 easy, 20 medium, 15 hard. Ten are graded on stopping and asking instead of paying or
  claiming. No task carries a reviewer sign-off yet.
- `tasks/schema.json`, a JSON Schema for whole payments task files, generated from `TaskSpec`
  and `PaymentsState` and kept in step with the code by a test.
- `worlds.payments.graders`: a check library (`common.py`: payments, spend, limits, duplicates,
  purchases, refunds, disputes, claims, mandates, subscriptions, asking, unauthorised tools) and
  one grader per task, each mapping its failure to a failure type.
- Golden and wrong runs as YAML data with `$refs` (`graders/runs/`), played through
  `kit.scripted.run_script` by `graders/runs.py`: every task has a golden run that passes and at
  least one plausible wrong run that fails with a declared failure type: 123 runs (50 golden,
  73 wrong).
- `history` in `initial_state`: past purchases and payments replayed when a task loads, so
  dispute tasks can start after the fact, including near the 45-day window.
- The `ask_user` tool (25 tools now), which ends the task with a question for the user.
- Failure types `payments.unconfirmed_payment` (high impact) and `payments.false_claim`.
- `sandbox tasks validate` checks each file against its world's `tasks/schema.json` when the
  world ships one, checks file names, category folders and duplicate ids, reports every bad file,
  and counts tasks awaiting sign-off.
- A reward-hacking review in `docs/DECISIONS.md`, with a test for every blocker
  (`test_reward_hacking.py`), plus tests for every check, the run player and history.

### Changed — Phase 3

- Tool arguments that name an id or a page now have a strict pattern, and services refuse a path
  with a `.` or `..` segment: an id like `../admin/state` reached the graders' admin view before.
- With history, the rails arm faults after the replay (`POST /internal/faults`), so
  `timeout_on_attempts` still counts from the agent's first payment.

### Added — Phase 2: commerce, disputes and MCP tools

- `worlds.payments.services.commerce`: merchants with five reliability profiles (honest, slow,
  wrong item, price change, unclear refund policy), products with variants and stock, plans,
  valid, expired and conditional offers, carts, orders that ship, arrive or arrive wrong,
  subscriptions billed through recurring mandates, invoices, receipts and merchant pages with an
  extension point for Phase 5's stress content.
- `worlds.payments.services.disputes`: refund requests merchants accept, partly accept or ignore
  until a deadline; disputes decided by fixed rules at a deadline on the `SimClock`; a pure
  reconciliation of payments against orders; and a support inbox with merchant replies and
  notices.
- 24 payments tools with pydantic-typed schemas (`tools/schemas.json`), deterministic error codes
  and a trace step with `state_diff` per call: the prompt's 21 plus `wait`,
  `change_subscription` and `resume_mandate`. A timed-out payment shows only its idempotency key.
- `PaymentsEpisode`, wiring rails, commerce and disputes with snapshot and restore across all
  three, and the `payments` world's entry point, failure types and `initial_state` schema.
- Rails: creating mandates, the account statement, and a payer's mandate list.
- `kit.mcp_bridge`: a world-neutral MCP server over any episode, behind the new kit extra
  `agent-sandbox-kit[mcp]` (`mcp==1.30.0`). `python -m worlds.payments.tools.server <task.yaml>`
  serves one task over stdio.
- Tests: one end-to-end episode per merchant profile and a subscription flow, all through a real
  MCP client session; a byte-identical determinism test across all three services; tool,
  commerce, disputes and reconciliation tests; and the bridge on the hello-world example.

### Changed — Phase 2

- `kit.service.SqliteStore` no longer ties its connection to the creating thread, so the MCP
  bridge can drive an episode from a worker thread; the bridge keeps calls serial.
- The rails' shared plumbing moved to `worlds.payments.services.common`; behaviour is unchanged.

### Added — Phase 1: simulated payment rails

- `worlds.payments.services.rails`: UPI-inspired rails over one SQLite database per episode —
  accounts with test-format handles, recurring, one-time and Reserve Pay-style mandates,
  per-transaction, daily, monthly and per-merchant caps, payments with idempotency keys, partial
  and full refunds, and a double-entry ledger with clearing and holds accounts.
- Seeded, configurable behaviour per task: timeouts where the payment really went through,
  lagging status checks, scheduled limit changes, Reserve Pay expiry.
- Internal REST API, read-only `/admin/*` views for graders, and `RailsService`, a kit
  `WorldState` that schedules its own events on the episode's `SimClock`.
- Tests: a scenario for every failure code, behaviour tests, hypothesis property tests (1,000
  cases each) for the ledger invariant and idempotency, a byte-identical determinism test, and a
  snapshot/restore timing test.
- CI type-checks `worlds.payments`; `worlds/payments/tests` is in `testpaths`.

### Added — Phase 0: repo and core kit

- uv workspace: `kit`, `stress`, `runner`, `worlds/payments`, `worlds/security` (the last two are
  empty packages until their phases).
- Core kit types: `TaskSpec`, `ToolCall`, `Observation`, `StepResult`, `CheckResult`,
  `GradeResult`, `FailureTypeDef`, `Signoff`, `Usage`.
- `WorldState`, `StateView`/`ReadOnlyService`, `Grader`, `Episode`, `WorldSpec` and a
  `WorldRegistry` driven by the `agent_sandbox.worlds` entry point.
- Namespaced failure-type registry with the kit's three generic types.
- `SimClock` with a scheduled-event queue, and `SeededRNG` with label-derived child streams.
- `Trace` with a `grader_view()` that cannot carry the agent's reasoning, plus JSONL read/write.
- `BaseEpisode`: step caps, allowed-tools enforcement, event draining, per-call trace steps with
  state diffs, snapshot and restore.
- Service harness: `JsonStore`, `SqliteStore`, and `AsgiService`, which passes simulated time to
  services through the `X-Sim-Time` header.
- OpenEnv adapter targeting `openenv==0.9.0`, exposing task sets as splits through the
  `TaskProvider` protocol. Optional extra: `agent-sandbox[openenv]`.
- `hello_world` example world: one service, three tools, one grader, two tasks, a golden run and
  two plausible wrong runs.
- `sandbox` CLI: `worlds`, `tasks validate`, `example`.
- CI: lint, strict type-check on the kit, tests, and a separate job for the determinism and
  domain-neutrality invariants.
