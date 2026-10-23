# Decisions

One line (or short block) per design decision, newest phase last. If you deviate from something
here, add a new entry rather than editing an old one.

## Phase 0 — repo and core kit (2026-10-18)

### Approved before coding

1. **Simulated time crosses the service boundary as a header.** Services are separate FastAPI apps
   and cannot share a Python clock, so they are told the episode's current simulated time on every
   request via `X-Sim-Time` (`kit.service.SIM_TIME_HEADER`) and never read a clock themselves. A
   service called without the header raises rather than guessing. In tests and CI services are
   mounted in-process over ASGI — fast, no ports, no races; docker-compose profiles are the
   alternate multi-process mode.
2. **Failure-type ids are namespaced** `<world>.<name>` (`payments.overspend`,
   `security.late_escalation`), with the kit owning `kit.unauthorised_action`, `kit.incomplete` and
   `kit.other`. Three worlds would otherwise collide on names like "late submission"; the
   leaderboard shows the short label.
3. **`state_diff` is emitted by the service** that performed the change, as
   `{op, path, before, after}` entries. The kit does not diff snapshots: it would be slow and would
   report noise instead of the rows the tool touched.
4. **"Graders never see the agent's reasoning" is a type, not a convention.** `Grader` receives a
   `GraderTrace`, whose steps have no `agent_message` field at all. `Trace.grader_view()` is the
   only way to produce one.
5. **`FailureTypeDef.high_impact` replaces payments-specific wording.** The scorer's "clean run"
   metric is "zero high-impact failures", so the kit needs no notion of money.
6. **Import roots are exactly those in the build plan**: `kit`, `runner`, `stress`,
   `worlds.<name>`. Distributions are `agent-sandbox-kit`, `-runner`, `-stress`,
   `-world-payments`, `-world-security`, with the umbrella `agent-sandbox` providing the `sandbox`
   CLI. Revisit before tagging `kit-v0.1` if the top-level `kit` name is a problem on PyPI: it is
   free to change now and expensive afterwards.

### Decided while building

7. **OpenEnv is an optional extra, pinned to `openenv==0.9.0`.** `meta-pytorch/OpenEnv` redirects
   to `huggingface/OpenEnv`; v0.9.0 is current. It is experimental upstream and pulls a large
   dependency tree (`harbor[...]` on Python 3.12), so `kit` must not depend on it: the adapter
   imports it lazily inside `environment_class()`, which is why the `Environment` subclass is built
   by a factory rather than declared at module level. Install with
   `pip install 'agent-sandbox[openenv]'` or `just setup-openenv`.
8. **The kit's `Episode` keeps its own `StepResult`; the adapter maps it to OpenEnv's shape.** In
   openenv 0.9.0 `step()` returns an observation carrying `done` and `reward`, not a tuple. The
   adapter also implements the optional `TaskProvider` protocol structurally, so a world's task
   sets become selectable splits (`payments-v0.1`, `payments-v0.1-hidden`).
9. **Rewards are computed inside the environment, as OpenEnv expects**: when an episode finishes,
   the adapter runs the task's grader and reports its score as that step's reward. No LLM-judge
   reward.
10. **`WorldSpec` carries `tasks_dir`** so the runner can find a world's packaged public tasks
    without importing the world. Added during Phase 0, before the interface freeze.
11. **A `WorldSpec` registers its failure types when it is constructed**, not when the registry
    loads it, so a grader can emit `hello_world.overshoot` in a unit test that never touches the
    registry.
12. **An unknown tool reports `unknown_tool`, not `tool_not_allowed`.** Calling a tool the world
    does not have is a mistake, but it is not an unauthorised action, and the two are counted
    differently.
13. **The hello-world example lives at `kit/src/kit/examples/hello_world/`**, not `kit/examples/`
    as the build plan sketched, so it is importable as `kit.examples.hello_world` and can be
    referenced by an entry point and by `grader:` paths. It deliberately uses no web framework, so
    discovering worlds never requires one.
14. **`AsgiService` drives the ASGI app through `asyncio.run` per request.** httpx 0.28 removed
    synchronous ASGI transport. Episodes are single-threaded and synchronous — that is part of what
    makes them reproducible — so one loop per request is acceptable; revisit in Phase 9's scale
    work.
15. **`mypy --strict` covers `kit/src/kit` only**; the rest is checked non-strictly. Packages ship
    `py.typed` so worlds and the runner see the kit's types.
16. **Each tool call advances simulated time by `BaseEpisode.step_seconds`** (default one second),
    so deadlines and SLAs mean something even in a world with no other scheduled events.

### Open, to settle before `kit-v0.1` is tagged

- Whether to namespace imports under `agent_sandbox.*` (decision 6).
- Reward shaping beyond the grader's score: currently the adapter reports `score` and nothing else.
- Whether `Trace` should carry per-step token usage for scheduled events (it currently does not).

## Phase 1 — simulated payment rails (2026-10-23)

Where public sources disagree or say nothing, the rule is a task setting with a sourced default.
Most UPI figures below come from payment-company docs and press reports of NPCI circulars, not the
circulars themselves; the payments reviewer should confirm them.

### Structure

17. **The rails live at `worlds/payments/src/worlds/payments/services/rails/`**, the system
    design's `services/rails/` in the `src` layout Phase 0 set up, not the phase prompt's shorter
    `worlds/payments/rails`.
18. **The rules are plain Python over SQLite (`core.py`); the FastAPI app (`app.py`) only
    translates HTTP.** That keeps the 1,000-case property tests fast. Every route is `async def`,
    because FastAPI runs plain `def` routes in a worker thread and the episode's in-memory SQLite
    connection belongs to the thread that created it.
19. **A service schedules its own events.** A `POST` response may carry `scheduled_events`;
    `RailsService` puts them on the episode's `SimClock`, and the episode hands due ones back
    through `RailsService.apply_event` (wired into `_on_event` in Phase 2). Events are
    `rails.settle`, `rails.limit_change` and `rails.reserve_expiry`.
20. **The snapshot is self-contained.** The task's rails config is stored in the database, and
    payment and refund ids come from `SeededRNG.child(f"payment/{n}")` keyed by row count, so ids
    depend only on the seed and the database, not on how many numbers were drawn before.

### Money and the ledger

21. **Money is integer paise, in one currency (INR).** UPI is domestic and rupee-only. Amounts are
    pydantic `StrictInt` and the SQLite tables are `STRICT`, so `10.5`, `10.0` and `"10"` are
    refused. If a future world needs other currencies, the pattern is the one Stripe and Razorpay
    use: amounts in the currency's smallest unit plus a `currency` field — a rename of `*_paise`
    plus exchange rules, not a patch.
22. **Double-entry with three system accounts.** `sys_issuance` goes negative by the opening
    balances, so all balances always sum to zero; `sys_clearing` holds in-flight money (pending
    payments); `sys_holds` holds Reserve Pay blocks. Every movement is two ledger entries. This is
    TigerBeetle's pending/posted idea with one balance per account instead of separate pending
    counters, so the invariant is a single sum.
23. **State diffs** cover created payments and refunds and changed balances, mandates and limits.
    Ledger rows are left out: they are implied by the balance diffs and would be noise.

### Payments

24. **Declines are recorded; rejections are not** (Stripe's rule). A payment that breaks a rule is
    saved as `failed` with a `failure_code` and a `failure_detail`; a malformed request
    (`invalid_request`, `invalid_amount`, `same_account`, `payer_not_found`,
    `idempotency_conflict`) is refused with nothing saved, so it can be retried.
25. **Idempotency.** Same key and same details returns the stored payment — including a stored
    failure — and writes nothing; same key with different details is `idempotency_conflict`
    (409). Keys never expire inside an episode, which removes Stripe's 24-hour pruning edge case.
26. **`payee_not_found` is a recorded failure**, so graders can see an attempt to pay a wrong
    address. `GET /handles/{handle}` checks an address first, as PayU and PhonePe's validate APIs
    do.
27. **Timeouts.** A timed-out payment is saved as `pending` with its money in clearing, and
    settles after `faults.settle_after_seconds` (default 90, NPCI's first status-check timer) to
    `faults.timeout_outcome` (`success` by default, or `reversed`). The internal API answers with
    `timed_out: true` and the true pending payment; the Phase 2 tool turns that into a timeout the
    agent sees. A retry with the same key, or `GET /payments?idempotency_key=`, is how the agent
    finds out what happened.
28. **Which attempts time out:** `faults.timeout_on_attempts` (scripted, for golden runs) and
    `faults.timeout_rate` (seeded, drawn from a child stream keyed by the attempt number).
29. **Status lag.** `faults.status_lag_seconds` (default 0) makes status lookups report `pending`
    until the payment settled plus the lag. The reply to the payment itself and the admin view
    always show the truth.
30. **Limits apply to every payment, mandate debits included**, and count successful and pending
    payments. "Daily" is a rolling 24 hours by default (strictly the last 24 hours) or
    `settings.daily_window: calendar_day` (midnight to midnight IST): Razorpay and HDFC describe a
    rolling window, other sources midnight. "Monthly" is the IST calendar month — an assumption,
    no public source found. The per-merchant cap is per payer, per payee, per daily window. A limit
    decline says the cap, what is used, what is left and `resets_at`, which real apps rarely do.

### Mandates and refunds

31. **`expired` is worked out from `end_at` when read, never stored**, so no write has to happen
    at the right moment. Stored statuses are `active`, `paused` and `revoked`.
32. **Mandate rules.** Recurring: each debit at most `max_amount_paise`, one debit per IST
    calendar period (day, ISO week, month), and an optional overall `remaining_limit_paise`.
    One-time: a single debit. `remaining_limit_paise` goes down with each debit and is restored if
    the debit is reversed.
33. **Reserve mandates hold the money**, as UPI Reserve Pay (single block, multiple debits) does.
    The block moves to `sys_holds` at load; debits are paid from it; revoking returns the rest at
    once; expiry returns it through a scheduled event. Default caps are ₹10,000 and 90 days
    (`settings.reserve_max_block_paise`, `settings.reserve_max_validity_seconds`).
34. **Payers can pause, resume and revoke** a mandate, as NPCI says UPI apps must allow. Creating
    or changing a mandate waits for Phase 2's tool design; Phase 1 mandates come from the task.
35. **Refunds need their own idempotency key.** The cap of total refunds ≤ the payment does not
    stop the same partial refund being sent twice after a timeout. Refunds succeed at once or fail
    (`refund_not_allowed`, `refund_exceeds_payment`, or `insufficient_funds` when the merchant
    cannot cover it).
36. **Admin views are read-only**: `/admin/state` plus `/admin/{accounts,mandates,limits,payments,
    refunds,ledger}`, with whitelisted filters, matching the kit's `query(resource)`. A test checks
    the snapshot is byte-identical after admin reads.

### Left out on purpose (candidates for the reviewer or later phases)

- The 20-transactions-per-day count cap, NPCI's "at most three status checks in two hours",
  merchant-category caps, 24-hour pre-debit notifications for recurring mandates, and retries of
  failed Reserve Pay debits. None is in the phase prompt.
