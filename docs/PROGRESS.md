# Progress

Paste this file into the next session along with the project-context block.

## Where we are

**Phase 0 — repo and core kit: built and green.** The interfaces are implemented, tested and
proven end to end by a hello-world world, including through the real OpenEnv adapter. They are
**not yet tagged** `kit-v0.1`: that waits on the pair leads' review.

**Phase 1 — simulated payment rails: built and green.** The rails are the first real use of
`AsgiService`, `SqliteStore` and the `X-Sim-Time` header. Of the phase's "Done when" list, the
property tests (1,000 cases each) and the byte-identical determinism test pass; the realism review
of behaviours and failure codes by a payments person is **pending** — it needs a person, not code.
Phase 1 was started before `kit-v0.1` was tagged, at the repo owner's request; it changes nothing
in the kit.

## What exists

| Area | State |
| --- | --- |
| uv workspace | `kit`, `stress`, `runner`, `worlds/payments`, `worlds/security`; Python 3.12, ruff, mypy, pytest, pre-commit |
| Core types | `TaskSpec`, `ToolCall`, `Observation`, `StepResult`, `CheckResult`, `GradeResult`, `FailureTypeDef`, `Signoff`, `Usage` |
| World contract | `WorldState`, `ReadOnlyService`/`StateView`, `Grader`, `Episode`, `WorldSpec`, `WorldRegistry` via the `agent_sandbox.worlds` entry point |
| Determinism | `SimClock` with a scheduled-event queue; `SeededRNG` with label-derived child streams; AST purity test over every source root |
| Tracing | `Trace`/`TraceStep` with state diffs, JSONL round-trip, and a `GraderTrace` that cannot carry reasoning |
| Episode loop | `BaseEpisode`: step cap, allowed-tools enforcement, event draining, snapshot/restore, one trace step per call |
| Services | `JsonStore`, `SqliteStore` (exact snapshots), `AsgiService` with the `X-Sim-Time` header |
| OpenEnv | Adapter against `openenv==0.9.0`; task sets exposed as splits; optional extra |
| Example world | `hello_world`: one service, three tools, one grader, two tasks, golden + two wrong runs |
| CLI | `sandbox worlds`, `sandbox tasks validate`, `sandbox example` |
| CI | `ci.yml` (lint, strict types on the kit, types on `worlds.payments`, tests) and `core-isolation.yml` (purity + no world imports) |
| Payments rails | `worlds.payments.services.rails`: `models.py` (task config and requests), `core.py` (rules over SQLite), `app.py` (internal REST + `/admin/*`), `service.py` (`RailsService`, a `WorldState` that schedules its own events) |
| Rails tests | 92 tests in `worlds/payments/tests`: one per failure code (19 declines, 13 rejections), behaviour, HTTP, two hypothesis properties at 1,000 cases, determinism, snapshot timing (≈0.05 ms snapshot, ≈0.1 ms restore with 2,000 payments) |

## Decisions made

See `docs/DECISIONS.md`, entries 1–36. Phase 0's six that shape everything: sim time travels as a
request header; failure types are namespaced per world; services emit their own state diffs; the
grader trace cannot carry reasoning; `high_impact` keeps the clean-run metric domain-neutral;
import roots stay as the build plan specified.

Phase 1's that later phases depend on:

- Declines are recorded as failed rows; rejections are not (24).
- Timeouts are pending payments that settle on a `SimClock` event; the internal API returns
  `timed_out: true` and the Phase 2 tool must present it as a timeout (27).
- Services schedule their own events through `scheduled_events` in `POST` responses (19).
- Money is single-currency integer paise (21).
- Reserve mandates hold funds in `sys_holds` (33).

## Open issues

1. **`kit-v0.1` is not tagged.** Pair leads must review the interfaces first. One question is still
   open: whether to namespace imports under `agent_sandbox.*` before the freeze (cheap now,
   expensive later).
2. **Payments realism review pending.** The behaviours and failure codes (listed in
   `worlds/payments/WORLD.md`) need a payments ops/risk person's judgment. Several defaults rest on
   secondary sources for NPCI circulars: the 90-second settle timer, ₹10,000 / 90-day Reserve Pay
   caps, the rolling 24-hour daily window. The payments world still has no named domain reviewer.
3. **`worlds/payments` has no entry point yet**, so it does not appear in `sandbox worlds`. That
   comes with its episode and tools in Phase 2. `worlds/security` is still an empty package.
4. **The rails' episode wiring is Phase 2's job**: load `initial_state["rails"]` with
   `RailsService.load`, and route `rails.*` events from `BaseEpisode._on_event` to
   `RailsService.apply_event`.
5. **The ASGI bridge runs one event loop per request** (httpx 0.28 removed sync ASGI transport).
   Fine for determinism, worth revisiting in Phase 9's scale work.
6. **The repository is public** at <https://github.com/prarabdha-soni/agent-sandbox>, and CI is
   green on `main`. Two placeholders still need real addresses: `conduct@example.invalid` in
   `CODE_OF_CONDUCT.md` and `security@example.invalid` in `SECURITY.md`.
7. **`uv.lock` was rewritten by uv 0.12.20**, which records `revision = 3` where the committed file
   had `revision = 5`. Harmless (CI does not run `uv sync --locked`), but contributors on different
   uv versions will see this line flip.
8. **Not yet built, by design**: stress modifiers (Phase 5), the model-driven `sandbox run`
   (Phase 4), `release-check` (Phase 6), hidden-set tooling (Phase 5). `scripts/check_no_hidden.py`
   guards the rule in the meantime.

## Next step

Phase 2 (commerce, disputes, MCP tools): build the commerce and disputes services on the same
`AsgiService` + `SqliteStore` pattern, the payments episode that wires them to the rails, the MCP
tools that wrap the rails' internal API, and the `payments` world's entry point. Failure types
register as `payments.*`.
