# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the release plan:
`v0.1` payments, `v0.2` security plus guard, `v0.3` trust layer plus drug safety.

## [Unreleased]

This section becomes **[0.1.0] — payments-v0.1** when the repo owner tags `payments-v0.1` and
`v0.1.0` (`docs/RELEASING.md`). The release gate does not pass yet: it waits on the payments
reviewer's sign-off, real model results and the real calibration report.

### Added — Phase 9, item 1: multi-agent tasks

- `payments-v0.1-agents`, a pre-release task set for v0.2 (not part of the v0.1 release; Phase 9
  item 4 folds it into `payments-v0.2`): 12 tasks in which the user's agent works beside a seller
  agent: 5 negotiations (within budget, a floor above budget, a seller that steers through its
  message, one that never answers, one that answers late), 3 invoice disputes (a padded line, a
  correct invoice, a seller that refuses to correct) and 4 agent-to-agent payments under
  mandates (a collection as agreed, above it, sent twice, above it and refused). Each has a golden
  run and one to three wrong runs (12 golden, 18 wrong). Graders score the outcome for the user,
  never who won (`graders/agents.py`); every `reviewer_signoff` is `null`.
- Kit (K4, K5; additive, on the `kit-v0.1` review list): `kit.roles` (`Role`, the `TurnTaking`
  protocol); `WorldSpec.roles` and `WorldSpec.task_sets` (extra sets as `<world>-<version>-<name>`).
- Payments: deals, seller invoices and collections under mandates in commerce (tables created only
  for a task with deals); a pre-debit notice 24 hours before a collection (RBI's e-mandate rule);
  tools `list_deals`, `make_offer`, `accept_offer`, `dispute_invoice` for the user's agent and
  `seller_desk`, `seller_counter`, `seller_accept`, `seller_invoice`, `seller_withdraw`,
  `seller_message` for the seller role (added to the unfrozen `tools-payments-v0.1` candidate);
  `initial_state.counterpart` and `commerce.deals`; category `agents`; the scripted seller
  (`counterpart.py`).
- Runner: `sandbox run --counterpart scripted|<model id>` plays the counterparts
  (`runner/counterpart.py`, prompt `counterpart/v1`): turns after each of the agent's calls, a
  model seller limited to 4 calls a turn and `max_steps` a run, caps over both agents, calls to
  the other role's tools refused; `result.json` gains `counterpart` (multi-agent runs only), the
  manifest `counterpart` (model counterparts only). `sandbox verify` re-derives every scripted
  counterpart call. `sandbox tasks validate` and `sandbox worlds` cover extra task sets; the
  tool reference marks counterpart-only tools.
- Docs: `docs/site/multi-agent.md`; concepts, task format, isolation and limitations updated.
- Tests: `kit/tests/test_roles.py`; `worlds/payments/tests/test_agents.py` (every run, the scripted
  seller against every run file, deal rules and edge cases) and `test_agents_reward_hacking.py`;
  `runner/tests/test_counterparts.py` (end to end, kill and resume, verify catching edited,
  dropped and invented seller moves, model sellers, refused cross-role calls, combined caps, the
  guard, refusals) and `test_v0_1_baseline.py` (every existing scripted run and four run
  directories made before this item reproduce byte for byte); a container verify of the set in
  `test_isolation_docker.py`.

### Not changed — Phase 9, item 1

- `payments-v0.1` and `frozen-v0.1.json`, every existing tool, schema and response, the trace
  format and `results-v0.1`. Single-agent run directories are byte for byte what they were.
- `sandbox export` refuses the agents set, and `sandbox run --isolation container` refuses
  multi-agent tasks for now (DECISIONS 271).

### Added — Phase 9, item 6: security of the sandbox itself

- `sandbox run --isolation container [--image IMG]`: each episode's world (tool handlers,
  services, grader) runs in its own locked-down Docker container: no network, read-only root
  filesystem with no tmpfs and no `/dev/shm`, every capability dropped, no new privileges, a
  non-root user (`65534:65534` when the runner is root), 1 CPU, 512 MiB with no swap, 128
  processes, 120 CPU-seconds, no logging driver, never a pull. The runner talks to it over MCP on
  stdio (`sandbox episode serve` inside); model calls and keys stay outside. The run directory is
  byte for byte the same as in process. A killed, timed-out or failing container makes an error
  run that a resume retries. `runner/isolation.py`, `docs/site/isolation.md`.
- `sandbox verify --isolation container` and `sandbox submission check --isolation container`
  replay recorded traces in the same container settings with read-only mounts only;
  `sandbox verify --json`.
- Tests: `runner/tests/test_isolation.py` (the worker as a local process: byte-identical runs,
  the trace merge, hung and crashing episodes, bad, linked, FIFO and oversized output, Docker
  missing, the guard refusal, a root runner) and `runner/tests/test_isolation_docker.py` (probes
  of the network, writes, privileges and every limit; an out-of-memory episode; every task of both
  worlds in a container, compared byte for byte and verified both ways); a `container` job in CI.
- Dependency scanning: `scripts/dependency_scan.py` over `uv audit` and `npm audit`, accepted
  advisories with reasons in `.github/dependency-scan-ignores.toml`, and the
  `dependency-scan.yml` workflow (dependency changes, main, weekly). `runner/tests/test_supply_chain.py`.
- Signed releases: `release.yml` signs every archive in `dist/` and the image with Sigstore
  (`actions/attest@v4.2.2`, SLSA build provenance; the image's pushed to GHCR) and keeps PEP 740
  attestations on PyPI; owner's repository only. How to verify: `docs/RELEASING.md`. No release
  has been signed yet.
- `just image`, `just test-containers`, `just audit`.

### Changed — Phase 9, item 6

- The results-submission check replays submitted traces in a container, from an image built only
  after its files-only step passed.
- The image pins its base images by digest, runs as a non-root user (`65532`) and does not write
  bytecode; `docker run` commands that mount `runs/` pass `--user "$(id -u):$(id -g)"`.
- `SECURITY.md`: disclosure through GitHub private vulnerability reporting (pending the owner; no
  address named yet), and the hardening status as it now stands.

### Fixed — Phase 9, item 6

- The Docker image builds again: `.dockerignore` left out `guard/` and `training/`, workspace
  members since Phases 7 and 8.

### Security — Phase 9, item 6

- Lock upgrades: brotli 1.2.1, certifi 2026.10.23, fastmcp and fastmcp-slim 3.4.11, virtualenv
  21.14.13. Accepted with reasons until a fix can be taken: filelock 3.32.7 (litellm's cap),
  pillow 12.3.0 (gradio's cap, openenv extra only), ajv 8.17.1 (the SDK's pin; `$data` unused).

### Added — Phase 8: the guard library (v0.2)

- `guard/`: policies (`guard-policy-v1`, YAML or JSON, versioned and hashed over RFC 8785
  canonical JSON) with budgets per action, day and month, a confirm threshold, counterparty and
  category lists, mandate rules, time windows and named world checks; packs
  `payments/default` and `security/default` (drug safety pending its world and reviewer).
- The layered decision (`guard.Guard`): hard rules first and final on a block, then a decision
  backend within a timeout, the stricter of the two; a timeout, an error, an invalid answer or
  low confidence becomes ask_user. In sandbox runs the hard rules use each task's own
  delegated limits, and the stricter of those and the pack wins.
- Decision backends in `guard/backends/` (moved from `training/`): `rules_only`, our model, Jev,
  Clef and a self-hosted Strands Decider through System One, OpenAI Decisions, LLM judges, and
  calibration.
- Hash-chained decision records (`guard-record-v1`) and `sandbox-guard verify`.
- A Python decorator (`guard.guarded`), an MCP proxy (`guard.mcp_proxy`, and
  `sandbox-guard proxy` over stdio) and `sandbox-guard policy list | check`.
- The TypeScript SDK (`guard/typescript/`) with the same policy format, decisions and records,
  passing the same shared test vectors (`guard/test-vectors/`, written by
  `scripts/guard_vectors.py`); a `guard-typescript` CI job.
- `sandbox run --guard <policy> --backend <name>`: the guard in front of the world's
  high-impact tools, a `decisions.jsonl` per run, and `sandbox verify` checking it.
- The Guard models track: `sandbox-train eval --guard '{world}/default'` and
  `sandbox-train report --runs ... --baseline-runs ...`, writing `with_guard` (task success with
  and without the guard, beside block and ask_user rates) into `guards/results.json`.
- `docs/site/guard.md`.

### Changed — Phase 8

- The `rules` backend is `rules_only`. `guard` depends on the kit; the runner depends on
  `guard[mcp]`.
- `guard-results-v0.1` gains `with_guard` (additive); run directories gain `guard` in
  `manifest.json` and `result.json` (additive). `sandbox export` refuses guarded runs.

### Added — Phase 7: guard-model dataset, baselines and evaluation

- `guard/`: the decision interface (`DecisionRequest`, `Decision` with its strict JSON Schema,
  `BackendResult`, `DecisionBackend`, `parse_decision`, `fail_closed`) that Phase 8's guard
  library will build on.
- `training/` and the `sandbox-train` command: the `actions-v0.1` dataset of labelled
  high-impact attempts from both worlds (labels from the graders, run just before and after each
  attempt), ten seeded task templates per world (`tasks/templates/*.json`), splits by task
  family with near-duplicate families merged, exact de-duplication, class balance in train, a
  manifest and a generated dataset card.
- Leakage checks: `sandbox-train dataset check` (canaries, hidden ids and hashes, families
  across splits, near-duplicates across splits; a new job in `no-hidden-leak.yml`) and the local
  `dataset hidden-check` and `--decontaminate` against the hidden set, counts only.
- Baselines behind one interface: rules only, two LLM judges (model ids set at run time), System
  One decision models (TypeSafe Jev, Cloudflare Clef, self-hosted Strands Decider) and OpenAI's
  Decisions API, each failing closed to ask_user.
- Our model: prompt format, a backend that reads its decision from the three answer openings'
  probabilities, SFT and GRPO scripts with configs for Qwen2.5-0.5B (free T4) and Qwen2.5-1.5B
  (rented GPU), the RL reward, and `requirements-train.txt` outside the workspace.
- Evaluation: metrics as means over families with bootstrap intervals, robustness under stress,
  calibration with a low-confidence ask_user threshold, dated prices, the report generator
  (opening with where our model loses) and the `guard-results-v0.1` leaderboard file.
- `docs/blog/guard-model.md` (pending real runs) and the model card.

### Changed — Phase 7

- `release.yml` runs only on `prarabdha-soni/agent-sandbox`: a fork never tries to publish.
- The kit's purity and core-isolation tests also cover `guard/` and `training/`.

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
