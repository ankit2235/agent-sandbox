# Progress

Paste this file into the next session along with the project-context block.

## Where we are

**Phase 0 — repo and core kit: built and green.** The interfaces are implemented, tested and
proven end to end by a hello-world world, including through the real OpenEnv adapter. They are
**not yet tagged** `kit-v0.1`: that waits on the pair leads' review.

**Phase 1 — simulated payment rails: built and green.** The property tests (1,000 cases each) and
the byte-identical determinism test pass; the realism review of behaviours and failure codes by a
payments person is **pending** — it needs a person, not code.

**Phase 2 — commerce, disputes and MCP tools: built and green.** Of its "Done when" list:

- [x] A scripted agent completes buy, refund and dispute flows end to end through MCP —
  `worlds/payments/tests/test_mcp_flows.py`, one episode per merchant profile plus a subscription,
  each through a real MCP client session.
- [x] Reset restores all three services exactly — `test_payments_determinism.py`: identical bytes
  in rails, commerce and disputes for the same seed and calls, identical traces, and `close()`
  returns every service to its baseline.
- [ ] **Pending, needs a person:** review and freeze the tool schemas as `tools-payments-v0.1`.
  The candidate is `worlds/payments/src/worlds/payments/tools/schemas.json`; a test fails if the
  code drifts from it.

Phase 2 adds two small world-neutral kit changes (decisions 37–39), made before `kit-v0.1` is
tagged at the repo owner's request.

**Phase 3 — tasks and graders (payments): built and green.** Of its "Done when" list:

- [x] 50 tasks validate against the schema — `uv run sandbox tasks validate`: 52/52 task files
  valid (50 payments, 2 hello-world), each also checked against `tasks/schema.json`.
- [x] Every task has a passing golden run and a failing wrong run in CI —
  `worlds/payments/tests/test_task_runs.py` plays all 123 runs (50 golden, 73 wrong); each wrong
  run must fail with the failure type it declares.
- [ ] **Pending, needs a person:** the payments reviewer signs off every grader.
  `reviewer_signoff` is `null` on all 50 tasks and `sandbox tasks validate` reports 50 awaiting
  sign-off. The world still has no named reviewer.
- [x] The reward-hacking review is written up in `docs/DECISIONS.md` (18 ways to game a grader,
  each with its blocker and a test). It found one real hole, fixed: an id like `../admin/state`
  reached the graders' admin view (decision 81).

**Phase 3S — security operations world (defensive only): built and green.** The second world
runs on the kit with no kit changes. Of its "Done when" list:

- [x] The world runs end to end on the kit, and needed no kit change. Two payments helpers were
  rewritten inside the world instead (decision 86, with a proposal to move the run player into
  `kit.scripted` in Phase 4). It is reached through the entry point, served over MCP by the kit's
  bridge, and validated by `sandbox tasks validate`.
- [x] Every task has a golden run and a wrong run in CI, and the determinism test passes:
  `worlds/security/tests/test_security_task_runs.py` plays all 147 runs (50 golden, 97 wrong),
  each wrong run failing with the failure type it declares. `test_security_determinism.py` checks
  byte-identical state in all five services and that `close()` restores them.
- [ ] **Pending, needs a person:** a SOC analyst signs off every grader and the benign look-alike
  stories. `reviewer_signoff` is `null` on all 50 tasks. No reviewer is named yet.
- [ ] **Pending, needs a real model:** the best tested model scores under about 50% on the
  stressed set. The stressed set `security-v0.1-stressed` and the calibration report exist now
  (Phase 5); a real model run does not.

**Phase 4 — runner, model adapters, tracing, scoring: built and green, for both worlds.** Of its
"Done when" list:

- [ ] **Pending, needs a real model:** a full run of 50 tasks × 3 runs with one cheap model,
  killed midway and resumed. Everything around it is built and tested with the fake models: a
  `sandbox run` of 50 payments tasks × 3 runs (`fake:golden`, concurrency 4) was killed with
  SIGKILL after 45 runs, and the same command finished the other 105, every one passing and
  reproducing under `sandbox verify`. `test_runs.py` checks that a killed and resumed directory is
  byte-identical to an uninterrupted one. The command for a real model is in the README.
- [x] `sandbox verify` reproduces every grade from the recorded traces:
  `runner/tests/test_verify.py` replays all 50 golden and all 50 wrong runs of each world and
  catches a tampered response, a tampered grade and a dropped action.
- [ ] **Pending, needs the leaderboard:** the exported files load in the Lovable leaderboard
  without changes. Stand-in, in CI: every exported file is validated against JSON Schemas
  (`runner/src/runner/schemas/`) that also accept the DATA FILES example from the leaderboard
  prompt, kept as a fixture (`runner/tests/fixtures/data_files_sample/`).
- [x] CI runs with the fake model only: no test calls a provider, and LiteLLM is an optional
  extra CI does not install.

Phase 4 made four small, additive kit changes, approved before coding (decisions 107–110).

**Phase 5 — stress library and hidden set: built and green, for both worlds, with no kit
change.** Of its "Done when" list:

- [x] Every modifier has tests: 7 payments and 5 security modifiers, plus the core machinery.
  For every public task and every modifier that applies (176 payments and 216 security pairs),
  the stressed task validates, its adjusted golden run passes, and each trap run fails with the
  failure type it declares. Tests: `stress/tests/test_stress_core.py`,
  `worlds/payments/tests/test_payments_stress.py` and
  `worlds/security/tests/test_security_stress_runs.py`.
- [ ] **Pending, needs real models:** every modifier appears in the calibration report. The
  generator (`sandbox stress calibrate`, `sandbox stress report`) is built and tested with the
  fake models (`runner/tests/test_calibrate.py`); the commands for 2–3 cheap models are in
  `docs/STRESS.md`.
- [ ] **Pending, needs real models:** the best tested model scores under about 50% on the
  stressed set (`payments-v0.1-stressed`, 33 tasks; `security-v0.1-stressed`, 50). The report
  states it per world.
- [x] The hidden set is loaded from the private repo only, with a CI check that no hidden task
  or canary exists in the public repo. The runner reads `<world>-<version>-hidden` only from
  `HIDDEN_TASKS_DIR`, and only if every task is hidden, carries one valid canary and matches the
  published hashes; a task marked hidden is refused from any other directory. The new
  `no-hidden-leak` workflow runs `scripts/check_no_hidden.py` over every tracked file and
  `sandbox hidden leaks`. `runner/tests/test_hidden.py` covers generation, hashes, loading, bad
  canaries, tampering, runs, the canary scan and the leak checks, on sets built in `tmp_path`.
- [x] The hidden sets exist. The repo owner generated both (17 tasks each, payments and
  security) with a secret seed in the private repository
  (`/home/ankit-kumar/Desktop/101/agent-sandbox-hidden`); `sandbox hidden check` passed for both
  worlds (every hidden task's golden and trap runs played, 0 problems, hashes match), and the two
  published hash lists (`tasks/hidden-v0.1.json` in each world) are committed in 9cc5f84.

**Phase 6 — v0.1 release: built and green; the release itself waits on people and real model
runs.** One kit change, approved (K1, decision 158). Of its "Done when" list:

- [ ] **Pending, needs a reviewer and real models:** `sandbox release-check payments-v0.1`
  passes. The gate is built (`runner/release.py`) and names each of its nine conditions. Today,
  with `HIDDEN_TASKS_DIR` set, 5 of 9 pass (`public-tasks`, `hidden-set` with 17 tasks and 41
  runs, `grader-runs` with 189 runs, `frozen`, `no-hidden`); the four that fail wait on people
  and models: `signoff` (the payments reviewer, 0 of 50 signed), `calibration` (no real
  report), `results` (no real model has run; at least 5 needed, 3 runs each) and `published`
  (no leaderboard files exported from real results). Every condition is also tested passing
  and failing (`runner/tests/test_release.py`).
- [ ] **Pending, needs the repo owner and an outside tester:** someone outside the team installs
  the package from PyPI and reproduces one model's score. Built and tested: the six wheels at
  0.1.0, installed from the local build into a clean virtualenv, run `sandbox tasks validate`
  (102/102) and the quickstart, and `verify` reproduces 10/10 runs (also a CI job). The Docker
  image builds and runs the quickstart. Publishing (PyPI trusted publishers, the `v0.1.0` tag,
  GHCR) is the owner's: `docs/RELEASING.md`.
- [ ] **Pending, needs real results and the Lovable site:** the leaderboard goes live with real
  results. No placeholder data is committed; `leaderboard/public/data/` is created only by
  `sandbox export` from verified real runs, and the gate's `published` condition fails unless
  it matches them.

Also built: the docs site (12 pages, `mkdocs build --strict` passes; Pages deploy pending the
owner), the results submission flow (submission file, `sandbox submission fetch | check |
spot-check`, template and workflow), the OpenEnv / Hugging Face Space bundle for payments
(`openenv validate` static and runtime pass; push pending the owner), and the write-up
(`docs/blog/v0.1.md`, findings pending real runs). The Prime Intellect verifiers wrapper is not
built, by the repo owner's decision (decision 159).

**Phase 7 — guard-model dataset, baselines, training configs, evaluation: built and green; no
model has been trained or run.** Built for payments and security; drug safety is recorded as
pending (its world does not exist). No kit code change; the kit's purity and isolation tests now
also cover `guard/` and `training/`. Of its "Done when" list:

- [x] A versioned action dataset and its card exist, and CI proves no hidden task, canary or
  near-duplicate across splits is in it. `actions-v0.1`, built locally with the full config and
  decontaminated against the hidden set: 20,430 tasks planned (100 public, 200 template
  variants, each also under one or two modifiers with three seeds), 84,390 attempts labelled,
  56,649 exact duplicates dropped, 0 label conflicts, 766 rows dropped as near-duplicates of
  hidden rows, **24,598 rows**: train 11,477 (48 family groups), validation 4,784 (12), test
  8,337 (12). `sandbox-train dataset check` finds 0 problems; `dataset hidden-check` finds 0
  near-duplicates and 0 canaries. The manifest and the generated card are in
  `training/data/actions-v0.1/`; the JSONL files (273 MB) stay in the ignored
  `training/data/out/`. CI's `training-data` job (in `no-hidden-leak.yml`) builds a small set
  with the same code and runs the check; the check against the hidden set itself is local
  (decision 184).
- [ ] **Pending, needs real models, provider access and a GPU:** our model and at least four
  baselines scored on held-out and hidden sets, with intervals, robustness and wrongful-block
  rates. Built and tested with fake and local stand-ins: the rules-only baseline (measured on
  the full build: `docs/blog/guard-model.md`), two LLM judges (model ids set at run time),
  System One decision models (Jev, Clef, a self-hosted Strands Decider), OpenAI's Decisions API,
  our model's backend, calibration, the metrics and the report. The commands are in
  `training/src/training/guard_model/README.md` and the write-up.
- [ ] **Pending, needs a trained model and publishing:** weights, model card and eval report
  published, including where our model loses. Prepared: `MODEL_CARD.md`, the dataset card,
  `docs/blog/guard-model.md` (the report generator opens with where our model loses), and the
  `guard-results-v0.1` leaderboard file format. Nothing is uploaded.
- [x] A leakage and reward-hacking review is in `docs/DECISIONS.md` (19 rows, each with its
  test). It found and fixed one real leak (authored idempotency keys such as `stress-bait` that
  only mistake runs used, decision 196) and one gap it could not fix in the data (every
  `reimage_host` attempt is a mistake).

Also: `release.yml` now runs only on the owner's repository (decision 212). Measured by the
build, not estimated: train prompts have a median of 6,158 characters, so the training configs
use 4,096 tokens and 10,771 of 11,477 train rows fit (decision 207).

## What exists

| Area | State |
| --- | --- |
| uv workspace | `kit`, `stress`, `runner`, `worlds/payments`, `worlds/security`; Python 3.12, ruff, mypy, pytest, pre-commit |
| Core types | `TaskSpec`, `ToolCall`, `Observation`, `StepResult`, `CheckResult` (with `failure_type`), `GradeResult`, `FailureTypeDef`, `Signoff`, `Usage` |
| World contract | `WorldState`, `ReadOnlyService`/`StateView`, `Grader`, `Episode`, `WorldSpec` (with `listing` and `runs_dir`), `Listing`, `MetricDef`, `WorldRegistry` via the `agent_sandbox.worlds` entry point |
| Determinism | `SimClock` with a scheduled-event queue; `SeededRNG` with label-derived child streams; AST purity test over every source root |
| Tracing | `Trace`/`TraceStep` with state diffs, JSONL round-trip, and a `GraderTrace` that cannot carry reasoning |
| Episode loop | `BaseEpisode`: step cap, allowed-tools enforcement, event draining, snapshot/restore, one trace step per call, `router` |
| Scripted runs | `kit.scripted`: `run_script`, and the YAML run player both worlds use (`RunFile`, `load_runs`, `resolve_refs`, `play`) |
| Services | `JsonStore`, `SqliteStore` (exact snapshots, usable from a worker thread), `AsgiService` with the `X-Sim-Time` header |
| MCP | `kit.mcp_bridge.McpBridge` (extra `[mcp]`, `mcp==1.30.0`): any episode's tools over MCP, filtered by `allowed_tools`, calls serialised |
| OpenEnv | Adapter against `openenv==0.9.0`; task sets exposed as splits; `reset` lists the task's tools; optional extra. Payments Space bundle in `worlds/payments/openenv/` (`openenv.yaml`, server, client, `finish_episode`) |
| Example world | `hello_world`: one service, three tools, one grader, two tasks, golden + two wrong runs |
| CLI | `sandbox worlds`, `sandbox tasks validate`, `sandbox example`, `sandbox run`, `sandbox verify`, `sandbox export`, `sandbox stress list/calibrate/report`, `sandbox hidden generate/hashes/check/scan/leaks`, `sandbox quickstart`, `sandbox release-check`, `sandbox freeze`, `sandbox submission fetch/check/spot-check`, `sandbox tools` |
| Stress library | `stress/core`: generic contracts (`catalogue.py`), seeded wording (`wording.py`), `Modifier`/`WorldStress` (`model.py`), `apply`, `stressed_set`, `per_modifier`, `scripted_runs` and the `agent_sandbox.stress` registry (`apply.py`) |
| Stressed and hidden sets | `<world>-<version>-stressed` (33 payments, 50 security tasks, in memory, seed 0); `<world>-<version>-hidden` from `HIDDEN_TASKS_DIR` with canaries and published hashes (`runner/hidden.py`); calibration (`runner/calibrate.py`); `docs/STRESS.md`, `docs/HIDDEN_SETS.md` |
| Runner | `agent.py` (agent loop over MCP, caps, versioned prompts in `prompts/`), `models.py` (LiteLLM adapter, `fake:golden`/`fake:wrong`/`fake:idle`), `runs.py` (run directory, resume, retries, concurrency), `tracing.py` (OpenTelemetry), `score.py` (bootstrap intervals), `export.py` + `schemas/` (`results-v0.1`), `verify.py` |
| Guard interface | `guard/`: `DecisionRequest`, `Decision` and its strict JSON Schema, `BackendResult`, `DecisionBackend`, `parse_decision`, `fail_closed` (Phase 8 builds the library on it) |
| Training | `training/` (`sandbox-train`): world training modules (`worlds/<name>/training.py`, entry point `agent_sandbox.training`), 10 templates per world (`tasks/templates/*.json`), labels from the graders, sources, near-duplicate detection, splits by family, build and manifest, leakage checks, dataset card; baselines (rules, LLM judge, System One, OpenAI Decisions) with configs; our model's format, backend, reward, SFT and GRPO scripts and configs, `requirements-train.txt`; metrics, prices, calibration, report and `guard-results-v0.1` schema |
| CI | `ci.yml` (lint, strict types on the kit on `worlds.payments` and `worlds.security`, tests; `package`: the built wheels in a clean virtualenv, quickstart), `core-isolation.yml` (purity + no world imports), `no-hidden-leak.yml` (no canary or hidden task in any tracked file, no copy of a published hidden task; `training-data`: a CI-sized action dataset and its leakage check), `docs.yml` (strict site build; Pages from main), `results-submission.yml` (results pull requests; maintainer spot-check) and `release.yml` (version tag: PyPI trusted publishing, GHCR image; owner's repository only) |
| Payments services | `services/rails` (Phase 1 + mandate creation, statement), `services/commerce`, `services/disputes` (with `reconcile.py`), shared `services/common.py` |
| Payments world | `merchants.py` (profiles), `state.py` (`initial_state` model with `history`, and schema), `links.py` (the only cross-service code), `episode.py` (`PaymentsEpisode`, `wait`, history replay), `tools/` (25 tools with `ask_user`, `schemas.json`, stdio server), `task_format.py` (task file schema), `stress/` (7 modifiers, `initial_state.stress_checks`); registered as world `payments` with `tasks_dir` set |
| Payments tasks | 50 in `tasks/<category>/` (15 rails, 15 commerce, 12 disputes, 8 multi; 15 easy, 20 medium, 15 hard), `tasks/schema.json` |
| Payments graders | `graders/common.py` (18 checks and `grade()`), one grader per task in `graders/{rails,commerce,disputes,multi}.py`, runs in `graders/runs/<id>.yaml` played by `graders/runs.py` |
| Security services | `services/{org,telemetry,alerts,response,tickets}` over the world's own `services/common.py`; containment logged with actor and impact; SLA timers on the clock |
| Security world | `vocab.py` (id patterns, enums, weights, RFC 5737/2606 checks), `scenarios/` (story format with ground truth, default company, seeded noise, 21 stories), `state.py`, `episode.py` (one simulated minute per call), `tools/` (17 tools, `schemas.json`, stdio server), `task_format.py`, `stress/` (5 modifiers, with runs and three guards in `grade()`) |
| Security tasks | 50 in `tasks/<category>/` (12 triage, 10 investigation, 10 containment, 8 reporting, 10 multi; 15 easy, 20 medium, 15 hard; 35 malicious, 15 benign), `tasks/schema.json` |
| Security graders | `graders/common.py` (goal checks and guards, `grade()` with impact penalty), one grader per task in `graders/{triage,investigation,containment,reporting,multi}.py`, 147 runs in `graders/runs/` |
| Packaging | Six distributions at 0.1.0 as `agent-sandbox-bench[-kit,-runner,-stress,-world-payments,-world-security]`, pinned to each other; the name in `runner.DISTRIBUTION`, `scripts/rename_distribution.py`; `Dockerfile` and `.dockerignore` (image with LiteLLM) |
| Release | `runner/release.py` (nine named conditions), `sandbox freeze` and `payments/tasks/frozen-v0.1.json`, `runner/validate.py` (shared task-file and run checks), `docs/RELEASING.md` |
| Submissions | `runner/submission.py` (`submission-v1` files, safe fetch, check, spot-check), `submissions/`, `.github/PULL_REQUEST_TEMPLATE/results_submission.md` |
| Docs | `mkdocs.yml`, `docs/site/` (quickstart, concepts, task format, graders, generated tool reference for both worlds, scoring, limitations, submitting results, contributing tasks, citation), `runner/reference.py`, `docs/blog/v0.1.md`, `docs/calibration/` |
| Tests | 2,719 in all, all passing with the openenv extra installed (CI, without it, skips the 7 OpenEnv adapter and 5 Space tests), 242 more than Phase 6: 164 in `guard/` and `training/` (`test_decision.py` 16, baselines 26, guard model 25, eval 20, templates 16, world 13, reward hacking 11, labels 10, leaks 10, similarity and splits 9, build 8), the rest the purity and core-isolation checks over the new source files. Before Phase 7: 2,477, 72 more than Phase 5. New in Phase 6: `test_release.py` (20: every gate condition passing and failing, freeze, quickstart, spot-check CLI), `test_submission.py` (20: every way of tampering with a submission, safe fetch, size cap, spot-check), `test_packaging.py` (6: names, versions, pins, what each built archive holds), `test_reference.py` (6), `test_openenv_space.py` (5), 2 in `test_openenv_adapter.py` (K1), 1 in `test_verify.py` (a run made before sign-off still verifies). Before Phase 6: 2,405 (2,404 pass, 1 skipped without the openenv extra), 700 more than Phase 4. New in Phase 5: `test_payments_stress.py` (381: every task × modifier, plus each modifier's content and edge cases), `test_security_stress_runs.py` (257), `test_hidden.py` (27), `stress/tests/test_stress_core.py` (15), `test_calibrate.py` (7). Before Phase 5: 1,705 (1,704 pass, 1 skipped without the openenv extra). Runner: 96 (agent loop, models, runs and resume, scoring, export and schemas, verify with all 50 tasks of both worlds, tracing, CLI). Before Phase 4: 1,600. Security: 782 (runs, reward hacking, stress, scenarios, services, tools, determinism, MCP). Before Phase 3S: 780 in all. Payments: 577. New in Phase 3: `test_task_runs.py` (135: every run plus task-file checks), `test_reward_hacking.py` (179), `test_graders_common.py` (23), `test_history.py` (13), `test_runs_player.py` (13). |

## Decisions made

See `docs/DECISIONS.md`, entries 1–213, the two reward-hacking reviews and the leakage and
reward-hacking review of the guard-model data. Phase 7's that later phases depend on:

- `guard.DecisionBackend` and the decision contract (178); every backend fails closed (178, 206).
- Labels from the graders before and after each attempt; ask_user versus block (194, 197).
- Splits by family group, near-duplicate families merged; hidden results by family bucket,
  buckets of fewer than three families as counts only (192, 202, 203, 209).
- Opaque idempotency keys (196); the training libraries outside uv (187).

Phase 6's that later
phases depend on:

- Published as `agent-sandbox-bench` (provisional, one constant, a rename script), 0.1.0, pinned;
  tags `payments-v0.1` and `v0.1.0` (150, 151).
- `task_hash` leaves out `reviewer_signoff`; hidden tasks follow their base task's sign-off
  (152, 153).
- Run directories are never committed; results come in as submission files, the leaderboard data
  is exported from verified runs only, and the archive goes on the GitHub release (154, 164, 166).
- The gate's nine conditions and thresholds (155, 163); the spot-check's invented 30-point
  tolerance (156); task content frozen by hash (165).
- K1: the OpenEnv reset lists the task's tools (158). K2 proposed: `finish_episode` in the kit
  adapter (171).

Phase 5's that later
phases depend on:

- Modifiers are `(task, seed) -> TaskSpec` with an `adjust` and a `trap`, registered per world
  through the `agent_sandbox.stress` entry point; `stress.core.apply` checks what they may not
  change (128–131, 134).
- Payments stress checks travel in `initial_state.stress_checks` and decide the failure type
  (135); security has three stress guards (140).
- The stressed set is seed 0, one modifier per task with a trap (142). Calibration verdicts at
  5 points and 0% / 50% (133, 141).
- The hidden set's layout, canaries, canonical-JSON hashes beside the world's tasks, and the
  loading rules (145–148). Rotation policy: `docs/HIDDEN_SETS.md`.

Phase 4's that later
phases depend on:

- `overall` is the mean over tasks of each task's pass rate, with a 95% bootstrap interval over
  tasks; `task_success_rate` is the share of tasks passed in every run (112). Mean grader score
  and `scope_recall` are later options (111, 112).
- `safe_action_rate` reads every failed check's failure type against the world's unsafe set
  (109, 113).
- The run directory format `sandbox-run-v1`; `result.json` last marks a finished run; resume
  refuses other settings; task hashes are checked (121).
- The leaderboard mapping, `worlds.json` from `WorldSpec.listing`, and the schemas in
  `runner/schemas/` (108, 124). Hidden runs only reach `metrics.hidden` (125).
- `sandbox export --updated` is required; no wall clock decides a date (116). The backoff sleep
  is the runner's only wall-clock call (120).

Phase 3S's that later phases depend on:

- A story carries its ground truth inside the task; no service ever holds it (88). Hidden tasks can
  carry inline stories (89).
- One simulated minute per tool call (91); SLA defaults 15/60/240/1,440 minutes, per task (93).
- Harm checks are guards that earn no credit; doing nothing scores 0 (101). Impact penalty 0.05 per
  point on healthy assets (92).
- A classification counts only after opening the ticket's alert (95). Only true positive vs not is
  graded (94).
- Stress modifiers are `(task, seed) -> TaskSpec` functions that inline and edit the story, so the
  ground truth moves with them; Phase 5's `stress.apply()` can call them unchanged (102).
- The YAML run player proposed for the kit (86) moved into `kit.scripted` in Phase 4 (107).

Phase 3's that later phases depend on:

- Tasks live inside the package (67); a world may ship `tasks/schema.json`, which
  `sandbox tasks validate` applies (68, 69).
- Runs are YAML with `$refs`, played through `kit.scripted.run_script` (70, 85).
- `history` replays the past at load; offsets count from when the agent starts; faults count from
  the agent's first payment (71, 72).
- `ask_user` ends the episode; a question counts only after the step that reveals the problem
  (74, 80).
- A row is the agent's only if its creation is in the trace; spend is gross; a declined attempt
  counts as trying (76–79).
- Ids in tool arguments have a strict pattern and services refuse dot segments (81).

Phase 2's that later phases depend on:

- Services never call each other; `links.py` carries everything across, and rules get facts handed
  in when they run (44, 45).
- `pay` is the only tool that moves the user's money; checkout only creates the order (48).
- Refused calls record nothing; declines and timeouts are recorded results with an `outcome` (47).
  A timeout shows only its idempotency key (42).
- `wait` drains due events at their own times inside its step and reports only new messages (41).
- `initial_state` is `{user, rails, commerce, disputes, history}`; the agent acts as `user` (46).
- Merchant profiles are default bundles with per-merchant overrides; promised and actual delivery
  differ (50, 51).
- Dispute and refund windows: 45 days to raise, 15 days to decide with deemed acceptance, 5 days
  for a merchant's refund answer; order payment window 15 minutes (54, 60–62).
- Phase 5's stress content goes in `commerce.page_content` and seeded inbox messages, as task data
  (59, 64).

## Open issues

1. **`kit-v0.1` is not tagged.** Pair leads must review the interfaces first, now including
   `kit.mcp_bridge` and the `SqliteStore` thread change. Still open: whether to namespace imports
   under `agent_sandbox.*` before the freeze.
2. **`tools-payments-v0.1` is not frozen.** A person must review `tools/schemas.json`: names,
   arguments, descriptions and error codes. Phase 3 changed it: `ask_user`, and id patterns on
   every argument that names an id or a page (decision 81).
3. **Payments realism review pending, and wider now.** Phase 1's rails defaults, plus Phase 2's:
   the dispute and refund windows rest on secondary reports of NPCI circulars (OC 184B/2025-26, the
   45-day window) and an analogy to RBI's T+5; the 15-minute payment window and 10-minute
   duplicate window are invented; the profile timings are judgment calls. The payments world still
   has no named domain reviewer.
4. **No grader is signed off.** All 50 tasks have `reviewer_signoff: null`. The reviewer should
   check each grader, its golden and wrong runs, and the failure-type mapping (for example,
   paying outside the mandate the user named is `payments.ignored_limit`).
5. **Not covered by a task yet:** first-order-only and product-specific offers, a merchant who
   contests a dispute the user gave no evidence for, weekly and daily plans, and the
   `account_frozen` and `payee_not_found` declines (DECISIONS, after 85).
6. **The ASGI bridge runs one event loop per request** (httpx 0.28 removed sync ASGI transport).
   Fine for determinism; a busy payments step now makes several service calls, so revisit in
   Phase 9's scale work.
7. **The repository is public** at <https://github.com/prarabdha-soni/agent-sandbox>, and CI is
   green on `main`. Two placeholders still need real addresses: `conduct@example.invalid` in
   `CODE_OF_CONDUCT.md` and `security@example.invalid` in `SECURITY.md`.
8. **`uv.lock` is written by uv 0.12.20** (`revision = 3`), so contributors on other uv versions
   may see that line flip. Phase 2 added `mcp` and its dependencies to the lock.
9. **The leaderboard's `stress` category is not built** (decision 133 had left it to Phase 6;
   the Phase 6 prompt does not ask for it, decision 176). It needs a `results-v0.1` schema change
   and real stressed results. `release-check` is built (Phase 6).
10. **The content of a question to the user is not graded** (accepted residual risk in the
    reward-hacking review): only that the agent asked, after looking at the right thing.
11. **Security world: no named SOC reviewer.** No grader or benign look-alike story is signed off;
    `tools-security-v0.1` is not frozen; the SLA table (decision 93), the impact weights
    (decision 92), the root-cause and "affected account" rules (97, 105), and each story's
    `optional_sources` (104) need the reviewer's confirmation.
12. **Payments scoring differs from security's.** Payments still scores the share of all checks, so
    doing nothing earns credit for harm checks there; decision 101 suggests the same guard rule
    for the payments reviewer to consider.
13. **One payments helper is still duplicated in the security world**: `services/common.py`
    (it needs FastAPI, which the kit must not). The run player is shared now (decision 107).
14. **No real model has been run yet.** The 50 × 3 run with a cheap model, killed and resumed,
    is the user's to run (README, "Running a model"). Nothing has measured real token use or
    cost per task, so `--max-cost` defaults are not set.
15. **The export is not yet loaded in the Lovable leaderboard.** It is validated against schemas
    that accept the leaderboard prompt's own example; the leaderboard itself must still load it.
16. **The lock moved `openai`, `huggingface-hub` and `filelock` to older majors** because of
    litellm's caps (decision 114). With the `openenv` extra installed from this lock, the OpenEnv
    adapter tests and the Space tests pass (Phase 6), so `openenv==0.9.0` works on it. CI still
    does not install the extra, so CI skips those 12 tests.
17. **Both worlds are `preview` on the leaderboard** until their reviewers sign off the graders.
    Security's `scope_recall` metric from the leaderboard sample is not computed (decision 111).
18. **Resolved: the hidden sets exist.** The repo owner generated both (17 tasks each) with a
    secret seed in the private repository, `sandbox hidden check` passed for both worlds, and the
    two published hash lists are committed (9cc5f84). They must never be regenerated for v0.1.
19. **The v0.1 hidden set is weak by construction**: seeded variants of public tasks under public
    modifiers (stated in `docs/HIDDEN_SETS.md`). It catches copied answers and leaks, not transfer
    to unseen jobs. Tasks written from scratch, signed off by reviewers, should replace it at the
    first rotation.
20. **No calibration has been run with real models**, so no modifier is yet known to be too easy
    or too hard, and "best model under about 50% on the stressed set" is unmeasured.
21. **Stress coverage gaps**: no payments modifier applies to the 12 `disputes` tasks (they do not
    allow `pay`); security implements no `misleading_content`, `pressure`, `flaky_service` or
    `shifted_constraint`. Stress numbers (decision 137) and the `shifted_constraint` window
    (decision 138) are invented, for the payments reviewer.
22. **Building a stressed set takes 10–20 s**, mostly in the kit's `validate_task`, which
    re-checks the JSON schema on every call. A cached validator needs an approved kit change
    (decision 149).

23. **The package name is provisional.** `agent-sandbox-bench` waits for the repo owner's
    confirmation (`scripts/rename_distribution.py` changes it); so does the image name
    `ghcr.io/prarabdha-soni/agent-sandbox` and the citation's author list.
24. **Nothing is published yet**, by rule: the PyPI trusted publishers, the `pypi` and
    `spot-check` environments, GitHub Pages, GHCR visibility, the `v0.1.0` tag, the release
    archive and the Hugging Face Space are the owner's steps (`docs/RELEASING.md`). The Space's
    `uv.lock` can only be made after PyPI.
25. **The Space's base image is `openenv-base:latest`**, as the OpenEnv template has it; not pinned
    to a digest. It ships Python 3.11; the Space installs 3.12 itself (decision 172).
26. **K2 proposed for `kit-v0.1`**: a `finish_episode` action in `kit.openenv_adapter`, which now
    lives in the payments Space only (decision 171). Add K1 (decision 158) to the review list.
27. **No verifiers wrapper** (decision 159): blocked by `mcp==2.0.0` in verifiers 0.4.x and the
    litellm/openai conflict with 0.3.0. Revisit when the kit moves to mcp 2.x.
28. **The spot-check's 30-point tolerance is invented** (decision 156), and a replay cannot tell a
    model's actions from hand-written ones; only the spot-check rerun can, and only on a sample.
29. **The `just` recipes added in Phases 6 and 7 were not run** (`just` is not installed on the
    build machine); each one is a single command that was run directly.
30. **No guard model is trained and no outside backend has run.** Every number in the eval
    report but the rules-only baseline's is pending: the judges (model ids and keys), Jev, OpenAI
    Decisions, Clef (keys), Strands Decider (a local server), our model (a GPU), latency on CPU
    and a small GPU, and cost per 1,000 decisions. The GPU memory and runtime figures are
    estimates; the first training step is a 200-step timing run.
31. **Provider terms on publishing benchmark results are unchecked**: OpenAI's services
    agreement could not be read (403) and TypeSafe's terms were not found. Check both, and
    Cloudflare's, before publishing any number.
32. **The labels rest on graders no reviewer has signed off**, and the always-block sets
    (decision 197) and the rules-only baseline's rules (decision 211) are invented, for the
    payments and SOC reviewers.
33. **Data gaps**: every `reimage_host` row is a mistake and every `checkout` row is allow;
    `create_mandate` has 2 rows; ask_user is rare (in security, 9 rows in validation and none in
    train or test); templates vary surface text only. New tasks, not more templates, close these.
34. **The hidden set shares families with the training data** (it is built from the public
    families): of its 122 rows, 101 come from families in train. Hidden results are reported by
    family bucket; a hidden set written from scratch (open issue 19) would fix it.
35. **K3 proposed**: an `observe` hook on `kit.scripted.play` (decision 195), which would let the
    label loop stop copying the run player's step loop.
36. **Unverified details**: Clef's REST response envelope (decision 208), whether `torch==2.14.1`'s
    CUDA 13 wheels support a T4, and the 3–3.5 characters-per-token estimate behind the 12,000
    character prompt cut.
37. **Owner follow-up**: a CI job on the owner's repository only that runs `sandbox-train dataset
    hidden-check` with a read-only deploy key for the private repository (decision 184).

## Next step

Everything left for v0.1 is people's, models' or the owner's (`docs/RELEASING.md`, in order):

- A payments reviewer signs off the 50 graders (and the stress modifiers and their checks), and
  freezes `tools-payments-v0.1`; refreeze the tasks if any change.
- Real model runs: 5–6 models × 50 tasks × 3 runs on `payments-v0.1` and on
  `payments-v0.1-hidden`, kept outside git; the calibration with 2–3 cheap models, its report
  committed in `docs/calibration/payments-v0.1/`; fix or drop any modifier marked `too_easy` or
  `too_hard`. The Phase 4 run (killed and resumed) is covered by these.
- Export the leaderboard files, run `sandbox release-check payments-v0.1 --results <dir>` until
  all nine conditions pass, confirm the package name, set up PyPI, Pages and the environments,
  tag `payments-v0.1` and `v0.1.0`, attach the public run archive, push the Space.
- Then: an outside tester installs from PyPI and reproduces one model's score; the leaderboard
  goes live with real results; the write-up's findings are filled in from the measured numbers.

For Phase 7, the user's to run (`training/src/training/guard_model/README.md`,
`docs/blog/guard-model.md`):

- the baselines on the test split and the hidden set: the two judges (`--set model=...`, two
  providers), Jev, OpenAI Decisions, Clef, Strands Decider (local server); fill in each price
  with its date;
- the Stage A pilot on a free T4 (a 200-step timing run first), then the 1.5B config on a
  rented GPU; calibrate on validation; evaluate; write the report from `sandbox-train report`;
- check the providers' terms; then the owner publishes the weights, cards, report and
  `guards/results.json`.

Phase 3P (drug safety) still starts only once the repo owner confirms a drug-safety physician as
reviewer; then its Phase 7 parts (`submit_case`, `close_case_invalid`, a seriousness downgrade)
join the dataset. Otherwise Phase 8 (the guard library, on `guard.DecisionBackend`). In
parallel, a SOC reviewer should sign off the security world's 50 graders and freeze
`tools-security-v0.1`.
