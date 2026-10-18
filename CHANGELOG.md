# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the release plan:
`v0.1` payments, `v0.2` security plus guard, `v0.3` trust layer plus drug safety.

## [Unreleased]

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
