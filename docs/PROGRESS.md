# Progress

Paste this file into the next session along with the project-context block.

## Where we are

**Phase 0 — repo and core kit: built and green.** The interfaces are implemented, tested and
proven end to end by a hello-world world, including through the real OpenEnv adapter. They are
**not yet tagged** `kit-v0.1`: that waits on the pair leads' review.

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
| CI | `ci.yml` (lint, strict types on the kit, tests) and `core-isolation.yml` (purity + no world imports) |

## Decisions made

See `docs/DECISIONS.md`, entries 1–16. The six that shape everything else: sim time travels as a
request header; failure types are namespaced per world; services emit their own state diffs; the
grader trace cannot carry reasoning; `high_impact` keeps the clean-run metric domain-neutral;
import roots stay as the build plan specified.

## Open issues

1. **`kit-v0.1` is not tagged.** Pair leads must review the interfaces first. One question is still
   open: whether to namespace imports under `agent_sandbox.*` before the freeze (cheap now,
   expensive later).
2. **No real services yet.** `AsgiService` and `SqliteStore` are proven by kit tests, but no world
   uses them; Phase 1 is the first real exercise of the sim-time header and the <50 ms snapshot
   requirement.
3. **`worlds/payments` and `worlds/security` are empty packages** with no entry point, so they do
   not appear in `sandbox worlds` yet.
4. **The ASGI bridge runs one event loop per request** (httpx 0.28 removed sync ASGI transport).
   Fine for determinism, worth revisiting in Phase 9's scale work.
5. **Placeholders to replace before launch**: the `conduct@example.invalid` and
   `security@example.invalid` contacts, and the GitHub URLs in `pyproject.toml`.
6. **Not yet built, by design**: stress modifiers (Phase 5), the model-driven `sandbox run`
   (Phase 4), `release-check` (Phase 6), hidden-set tooling (Phase 5). `scripts/check_no_hidden.py`
   guards the rule in the meantime.

## Next step

Phase 1 (simulated payment rails) and Phase 2 (commerce, disputes, MCP tools) can start in
parallel once the interfaces are reviewed and tagged. Both build on `AsgiService` + `SqliteStore`
and must register their failure types as `payments.*`.
