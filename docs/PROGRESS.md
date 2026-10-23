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
- [ ] **Pending, needs Phase 4, Phase 5 and a real model:** the best tested model scores under
  about 50% on the stressed set. The five modifiers exist and are tested; the runner, the stressed
  set and a model run do not.

## What exists

| Area | State |
| --- | --- |
| uv workspace | `kit`, `stress`, `runner`, `worlds/payments`, `worlds/security`; Python 3.12, ruff, mypy, pytest, pre-commit |
| Core types | `TaskSpec`, `ToolCall`, `Observation`, `StepResult`, `CheckResult`, `GradeResult`, `FailureTypeDef`, `Signoff`, `Usage` |
| World contract | `WorldState`, `ReadOnlyService`/`StateView`, `Grader`, `Episode`, `WorldSpec`, `WorldRegistry` via the `agent_sandbox.worlds` entry point |
| Determinism | `SimClock` with a scheduled-event queue; `SeededRNG` with label-derived child streams; AST purity test over every source root |
| Tracing | `Trace`/`TraceStep` with state diffs, JSONL round-trip, and a `GraderTrace` that cannot carry reasoning |
| Episode loop | `BaseEpisode`: step cap, allowed-tools enforcement, event draining, snapshot/restore, one trace step per call |
| Services | `JsonStore`, `SqliteStore` (exact snapshots, usable from a worker thread), `AsgiService` with the `X-Sim-Time` header |
| MCP | `kit.mcp_bridge.McpBridge` (extra `[mcp]`, `mcp==1.30.0`): any episode's tools over MCP, filtered by `allowed_tools`, calls serialised |
| OpenEnv | Adapter against `openenv==0.9.0`; task sets exposed as splits; optional extra |
| Example world | `hello_world`: one service, three tools, one grader, two tasks, golden + two wrong runs |
| CLI | `sandbox worlds` (lists `hello_world`, `payments`, `security`), `sandbox tasks validate`, `sandbox example` |
| CI | `ci.yml` (lint, strict types on the kit, types on `worlds.payments` and `worlds.security`, tests) and `core-isolation.yml` (purity + no world imports) |
| Payments services | `services/rails` (Phase 1 + mandate creation, statement), `services/commerce`, `services/disputes` (with `reconcile.py`), shared `services/common.py` |
| Payments world | `merchants.py` (profiles), `state.py` (`initial_state` model with `history`, and schema), `links.py` (the only cross-service code), `episode.py` (`PaymentsEpisode`, `wait`, history replay), `tools/` (25 tools with `ask_user`, `schemas.json`, stdio server), `task_format.py` (task file schema); registered as world `payments` with `tasks_dir` set |
| Payments tasks | 50 in `tasks/<category>/` (15 rails, 15 commerce, 12 disputes, 8 multi; 15 easy, 20 medium, 15 hard), `tasks/schema.json` |
| Payments graders | `graders/common.py` (18 checks and `grade()`), one grader per task in `graders/{rails,commerce,disputes,multi}.py`, runs in `graders/runs/<id>.yaml` played by `graders/runs.py` |
| Security services | `services/{org,telemetry,alerts,response,tickets}` over the world's own `services/common.py`; containment logged with actor and impact; SLA timers on the clock |
| Security world | `vocab.py` (id patterns, enums, weights, RFC 5737/2606 checks), `scenarios/` (story format with ground truth, default company, seeded noise, 21 stories), `state.py`, `episode.py` (one simulated minute per call), `tools/` (17 tools, `schemas.json`, stdio server), `task_format.py`, `stress/` (5 modifiers) |
| Security tasks | 50 in `tasks/<category>/` (12 triage, 10 investigation, 10 containment, 8 reporting, 10 multi; 15 easy, 20 medium, 15 hard; 35 malicious, 15 benign), `tasks/schema.json` |
| Security graders | `graders/common.py` (goal checks and guards, `grade()` with impact penalty), one grader per task in `graders/{triage,investigation,containment,reporting,multi}.py`, 147 runs in `graders/runs/` |
| Tests | 1,600 in all (1,599 pass, 1 skipped without the openenv extra). Security: 782 (runs, reward hacking, stress, scenarios, services, tools, determinism, MCP). Before Phase 3S: 780 in all. Payments: 577. New in Phase 3: `test_task_runs.py` (135: every run plus task-file checks), `test_reward_hacking.py` (179), `test_graders_common.py` (23), `test_history.py` (13), `test_runs_player.py` (13). Runner: 12 CLI tests |

## Decisions made

See `docs/DECISIONS.md`, entries 1–106 and the two reward-hacking reviews. Phase 3S's that later
phases depend on:

- A story carries its ground truth inside the task; no service ever holds it (88). Hidden tasks can
  carry inline stories (89).
- One simulated minute per tool call (91); SLA defaults 15/60/240/1,440 minutes, per task (93).
- Harm checks are guards that earn no credit; doing nothing scores 0 (101). Impact penalty 0.05 per
  point on healthy assets (92).
- A classification counts only after opening the ticket's alert (95). Only true positive vs not is
  graded (94).
- Stress modifiers are `(task, seed) -> TaskSpec` functions that inline and edit the story, so the
  ground truth moves with them; Phase 5's `stress.apply()` can call them unchanged (102).
- Proposed kit change, not made: move the YAML run player into `kit.scripted` in Phase 4 (86).

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
9. **Not yet built, by design**: the model-driven `sandbox run` (Phase 4), the generic
   `stress/core` and hidden-set tooling (Phase 5; the security world's own five modifiers exist),
   `release-check` (Phase 6). `scripts/check_no_hidden.py` guards
   the hidden-task rule in the meantime.
10. **The content of a question to the user is not graded** (accepted residual risk in the
    reward-hacking review): only that the agent asked, after looking at the right thing.
11. **Security world: no named SOC reviewer.** No grader or benign look-alike story is signed off;
    `tools-security-v0.1` is not frozen; the SLA table (decision 93), the impact weights
    (decision 92), the root-cause and "affected account" rules (97, 105), and each story's
    `optional_sources` (104) need the reviewer's confirmation.
12. **Payments scoring differs from security's.** Payments still scores the share of all checks, so
    doing nothing earns credit for harm checks there; decision 101 suggests the same guard rule
    for the payments reviewer to consider.
13. **Two payments helpers are duplicated in the security world** (`services/common.py`,
    `graders/runs.py`), by design until the kit change proposed in decision 86 is approved.

## Next step

Phase 3P (drug safety) starts only once the repo owner confirms a drug-safety physician as
reviewer. Until then, Phase 4 (runner, tracing and scoring): the reference agent over the MCP tools, `sandbox run`
with caps and resumability, JSONL and OpenTelemetry traces, scoring with bootstrap intervals,
the leaderboard export, and `sandbox verify`, which can replay recorded actions the way
`graders/runs.py` plays scripted runs (decision 86 proposes moving that player into
`kit.scripted`). In parallel, a payments reviewer and a SOC reviewer should sign off their 50
graders each and freeze `tools-payments-v0.1` and `tools-security-v0.1`.
