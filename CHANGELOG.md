# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the release plan:
`v0.1` payments, `v0.2` security plus guard, `v0.3` trust layer plus drug safety.

## [Unreleased]

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
