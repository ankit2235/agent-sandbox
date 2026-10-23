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

## Phase 2 — commerce, disputes and MCP tools (2026-10-23)

### Approved before coding

37. **`kit.mcp_bridge` serves any episode's tools over MCP**, world-neutral, so the security world
    reuses it unchanged. `tools/list` shows only the task's `allowed_tools`; every `tools/call`
    goes through `Episode.step`, so caps, events and the trace step are the same as in a scripted
    run. A `threading.Lock` inside the bridge applies calls one at a time, because the SDK handles
    requests concurrently and an episode is strictly sequential. The SDK's own input validation is
    switched off, so a call with bad arguments still reaches the episode and is refused *and*
    traced. Run a task's server with `python -m worlds.payments.tools.server <task.yaml>`.
38. **New dependency: `mcp==1.30.0`**, the official MCP Python SDK (already resolved in `uv.lock`
    through the openenv extra). It is a kit extra, `agent-sandbox-kit[mcp]`, so the kit itself stays
    light; the payments world depends on the extra. Installing it added `mcp`, `cffi`,
    `cryptography`, `httpx-sse`, `pycparser`, `pydantic-settings`, `pyjwt`, `python-dotenv`,
    `python-multipart` and `sse-starlette`.
39. **`SqliteStore` opens its connection with `check_same_thread=False`.** `AsgiService` cannot run
    inside an event loop, so the bridge runs each call in a worker thread. Access stays serial
    (decision 37's lock), which is what SQLite needs.
40. **Three tools beyond the phase prompt's 21, added deliberately:** `wait` (deliveries, replies
    and deadlines are days away and each step is one simulated second, so without it no time-based
    flow can finish); `change_subscription` (the prompt asks for upgrade, downgrade and cancel, and
    no listed tool does them); `resume_mandate` (the rails already support it, and without it a
    paused mandate is a dead end).
41. **`wait` delivers the events that fall due inside its own step.** `BaseEpisode.step` drains
    events *before* a tool runs, so `PaymentsEpisode` handles `wait` itself: the clock stops at
    each due event's own time and drains it, so a delivery due on day 3 of a week-long wait happens
    on day 3, and its trace step comes before the `wait` call. The agent is told what a person
    would notice (new inbox messages), never the events: their payloads would leak a timed-out
    payment's id. No kit change.
42. **A timed-out payment shows the agent only `outcome: timeout`, `code: payment_timeout` and the
    idempotency key** — no payment id, no status (decision 27). The true pending payment is in the
    step's `state_diff` for graders. Retrying with the same key, or `get_payment_status` by key, is
    how the agent finds out.

### Structure

43. **Services live at `services/commerce/` and `services/disputes/`**, next to the rails. What all
    three share moved to `services/common.py`: `SqlCore` (transactions and query helpers),
    `Result`, `WorldService` (an `AsgiService` that schedules the events a `POST` asks for, and
    turns refusals back into `ToolRejected`) and the error handlers. The rails now use it; their
    behaviour is unchanged and all Phase 1 tests pass.
44. **Services never call each other.** `links.py` is the only code that carries information
    between them: a payment made for an order is linked to it in commerce; a timed-out payment that
    settles is reported to commerce, as a payment webhook would be; a renewal is collected through
    the rails with the user's mandate; a refund a merchant agrees to, or a won dispute, is paid
    through the rails and reported back; commerce's notices go to the inbox. Every snapshot is
    self-contained, and the episode's snapshot is the three of them.
45. **A rule that depends on another service's state is handed the facts when it runs.** A refund
    response or a dispute decision gets the order's status, whether it is late, what is still
    refundable and whether the payment is a duplicate, computed by `links.py` at that moment.
46. **`initial_state` is `{user, rails, commerce, disputes}`**, validated by `PaymentsState`, which
    also checks what spans services: the user and every merchant are rails accounts of the right
    kind, and a seeded subscription uses a recurring mandate the user gave that merchant. The
    agent always acts as `user`, and only sees its own payments and mandates; anyone else's are
    "not found".

### Tools

47. **Refused vs recorded** (decision 24 again). A refusal (`ToolRejected` with a code) records
    nothing and can be retried. A declined payment or a timeout *is* recorded, so it comes back as
    a normal result with an `outcome` and a `code`: the kit drops a step's `state_diff` when it is
    an error, and graders need it. Bad arguments are refused as `invalid_arguments`.
48. **`pay` is the only tool that moves the user's money.** `checkout` creates the order and a
    payment request (UPI's intent pattern) but does not pay, so the agent sees the checkout total
    before paying, and the trust layer later has one action to check. Paying with `order_id`
    checks the payee and amount match the order (`order_mismatch`), the order is unpaid
    (`order_already_paid`) and its window is open (`order_not_payable`).
49. **Schemas come from pydantic argument models** (`tools/args.py`), the same models that validate
    the call. The model's own title and docstring are left out of the schema the agent sees.
    `tools/schemas.json` is the record for review, and a test fails if code and file disagree.
    Freezing it as `tools-payments-v0.1` waits for a reviewer.

### Commerce

50. **Merchant profiles are default bundles** (`merchants.py`); a task can override any single
    behaviour per merchant. Commerce and disputes load the same `MerchantSeed`, so they cannot
    disagree about a merchant.
51. **Promised and actual delivery differ.** An order's `expected_delivery_at` is what the terms
    promise; a slow merchant promises 5 days and takes 9. "Not received" is judged against the
    promise — without this, no order could ever be late.
52. **Wrong items:** a scripted list of the merchant's paid orders (`wrong_item_on_orders`, for
    golden runs) or a seeded `wrong_item_rate` (0.5 for the `wrong_item` profile), decided at
    payment from a stream keyed by merchant and paid-order count. A wrong item is another variant,
    or "an item that is not" the product.
53. **Price change:** a `price_change` merchant raises a product's price by `price_change_bps`
    (default 1,500 = 15%) at its first checkout, for good. The cart keeps the price shown when the
    line was added, so the difference is visible to an agent that compares.
54. **The order payment window is invented: 900 seconds**, a setting
    (`commerce.settings.order_payment_window_seconds`). No public source fixes it. Stock is
    reserved at checkout; an unpaid order is cancelled at the window's end and its stock released,
    unless a payment for it is still in flight.
55. **Paying an order twice is possible and recorded**, because that is the mistake worth catching
    (a timeout retried with a new key). Once an order is paid, `pay` with its `order_id` is
    refused; a second payment that was already in flight is kept as a duplicate.
56. **Offers** can be valid, expired, or conditional (minimum cart, specific products, first order
    only). They are checked when applied and again at checkout, where one that no longer applies is
    dropped with a note. Offers never apply to plans. A fully discounted order is paid without a
    payment.
57. **Subscriptions follow UPI Autopay:** check out a plan, create a recurring mandate, then
    `pay(order_id, mandate_id)`. Renewals are merchant-initiated mandate debits one IST calendar
    period later, each recorded as a renewal order. A plan change applies from the next renewal;
    cancel is immediate. A declined renewal makes the subscription `past_due`, with no retries.
58. **Invoices are issued at checkout and receipts at payment**, as structured rows with rendered
    text, and delivered to the inbox.
59. **Pages are generated from the merchant's real behaviour** (shipping times, refund answers),
    unless the task replaces a page. `commerce.page_content` (`append`, `prepend` or `replace`) is
    applied last: the extension point for Phase 5's stress content, as plain task data.

### Disputes

Sources: RBI circular RBI/2019-20/67 (20 September 2019) on turnaround times; NPCI circular
NPCI/UPI/OC No. 184B/2025-26 (20 June 2025, effective 15 July 2025) as reported by TaxGuru; press
and blog reports of NPCI's uniform 45-day chargeback window. Card-network rules are not used. The
payments reviewer should confirm each figure against the circulars themselves.

60. **Refund requests** go to the merchant with a deadline, `refund_response_seconds`, default
    5 days. That borrows RBI's T+5 for UPI payments to merchants (RBI/2019-20/67, row 4b), which is
    about failed confirmations, not merchant refunds: an analogy, not a rule. A merchant pays back
    a valid claim — wrong item, not received by the promised date, or a duplicate payment — in
    full (`accept`) or in part (`partial`), declines anything else, or ignores the request until it
    expires (`ignore`). One open request per order.
61. **Disputes can be raised up to 45 days after the payment** (`dispute_window_seconds`, NPCI's
    reported uniform window) and are decided once, when the merchant's response time ends:
    `dispute_response_seconds`, default 15 days, NPCI's P2M response window. One dispute per
    payment; evidence is accepted until the decision.
62. **Dispute rules.** It is won only if all hold: the fact is true in the world (the order is
    `wrong_item`; it is not delivered after its promised date; the payment is a duplicate), the
    evidence points at the right object (the order; for a duplicate, the original payment), and
    for wrong-item and not-received disputes the merchant was asked for a refund first
    (`require_merchant_contact`). A merchant with `contests_disputes: false` never answers, which
    NPCI treats as deemed acceptance: only the fact is checked. A won dispute is refunded through
    the rails as a chargeback, for at most what is still unrefunded.
63. **Reconciliation is a pure function** (`services/disputes/reconcile.py`) over the rails' and
    commerce's admin rows. A duplicate is a second successful payment for the same order, or for the
    same payee and amount within 10 minutes (an invented window); the earliest payment is the
    original. It also reports amount mismatches and successful payments to a merchant with no order.
    Graders and the duplicate-charge rule use it; the agent only gets the raw statement.
64. **Inbox:** reading marks messages read (a recorded change); merchants reply after
    `reply_after_seconds`, clearly or vaguely by profile; a task can seed messages now or later.

### Rails additions

65. **`POST /mandates` creates a mandate starting now** (decision 34's deferred design). A reserve
    mandate blocks its amount at once and expires through the usual event. A mandate that breaks a
    rule is refused with nothing recorded (`invalid_mandate`, `insufficient_funds`): setting one up
    is a request the app refuses, not a payment that fails. Ids are `mdt_` plus a seeded token.
66. **The statement** (`GET /accounts/{handle}/transactions`) lists payments in and out and
    refunds, newest first, with lagged statuses like a status check. A declined payment shows on
    the payer's statement only. `GET /accounts/{handle}/mandates` lists the mandates a payer gave.
    The admin `payments` view can be filtered by `id`.

### Left out on purpose

- Seeding past orders and payments in `initial_state` (Phase 3's dispute tasks may want it), item
  returns, merchant-initiated cancellation, renewal retries, pre-debit notices, more than one user.
