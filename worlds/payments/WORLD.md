# World: payments

**Status:** rails (Phase 1), commerce, disputes and the MCP tools (Phase 2) built; tasks and
graders to come (Phase 3). **Version:** `payments-v0.1` (planned). **Tools:**
`tools-payments-v0.1`, schemas in `src/worlds/payments/tools/schemas.json`, **not frozen yet** —
they wait for review.
**Domain reviewer:** to be confirmed (payments operations / risk). No reviewer, no merge of a
grader.

## What it simulates

UPI-inspired payment rails built from public concepts only: accounts and test-format handles,
mandates (recurring, one-time, reserve-style pre-authorised limits), per-transaction, daily,
monthly and per-merchant caps, payments with idempotency keys, refunds, and a double-entry ledger.
Alongside them: merchants with reliability profiles, catalogues, offers, carts, orders,
subscriptions, invoices and pages; and after-payment operations — refund requests, disputes,
reconciliation and a support inbox.

The failures are the point: insufficient funds, limit exceeded, expired or revoked mandates, payee
not found, timeouts where the payment actually succeeded, lagging status checks, caps that change
mid-episode, prices that rise at checkout, wrong items, late deliveries, and merchants that answer
slowly, partly or not at all.

## Services

Three FastAPI services, each over its own SQLite database, snapshotted together. They never call
each other; `links.py` carries what one needs from another (`docs/DECISIONS.md`, 43–45).

- **`rails`** (Phase 1, extended in Phase 2): accounts, mandates, limits, payments, refunds,
  ledger. Phase 2 added creating mandates, the statement and a payer's mandate list.
- **`commerce`**: merchants, products with variants and stock, plans, offers, carts, orders
  (`created → paid → shipped → delivered | wrong_item`, or `cancelled`), subscriptions billed
  through mandates, invoices and receipts, and pages.
- **`disputes`**: refund requests, disputes with evidence and a decision at a deadline,
  reconciliation, and the inbox.

### Merchant profiles

A task picks a profile per merchant and may override any single behaviour.

| Profile | Ships / promises / delivers | Wrong item | Price at checkout | Refund request | Replies |
| --- | --- | --- | --- | --- | --- |
| `honest` | 1 / 3 / 3 days | never | as in the cart | accepts in full after 1 day | 2 hours, clear |
| `slow` | 4 / 5 / 9 days | never | as in the cart | accepts after 4 days | 3 days, clear |
| `wrong_item` | 1 / 3 / 3 days | seeded, 50% | as in the cart | accepts in full after 1 day | 2 hours, clear |
| `price_change` | 1 / 3 / 3 days | never | +15% | accepts in full after 1 day | 2 hours, clear |
| `unclear_refund_policy` | 1 / 3 / 3 days | never | as in the cart | half, after 2 days; vague policy page | 1 day, vague |

A merchant only refunds a valid claim: a wrong item, an order not delivered by its promised date,
or a duplicate payment.

### Default windows

| Setting | Default | Basis |
| --- | --- | --- |
| Order payment window | 15 minutes | invented; a setting |
| Merchant answers a refund request | 5 days | RBI's T+5 for UPI payments to merchants, by analogy |
| Raise a dispute | 45 days after payment | NPCI's uniform chargeback window, as reported |
| Dispute decided | 15 days after it is raised | NPCI's P2M response window; no answer is deemed acceptance |
| Duplicate: same payee and amount | within 10 minutes | invented |

## Tools (`tools-payments-v0.1`, pending review)

The agent's only interface, served over MCP (`python -m worlds.payments.tools.server
<task.yaml>`), filtered per task by `allowed_tools`. Amounts are integer paise.

- **Read:** `get_balance`, `list_transactions`, `get_payment_status`, `list_mandates`,
  `get_limits`, `search_products`, `read_page`, `view_cart`, `get_order`, `read_inbox`.
- **Act:** `add_to_cart`, `apply_offer`, `checkout`, `pay`, `create_mandate`, `pause_mandate`,
  `resume_mandate`, `revoke_mandate`, `request_refund`, `open_dispute`, `submit_evidence`,
  `send_message`, `change_subscription`, `wait`.

`pay` is the only tool that moves the user's money. A refused call records nothing and carries a
code; a declined or timed-out payment is recorded and comes back with an `outcome`. A timeout shows
only the idempotency key.

## Failure codes, for the realism review

- **Rails, recorded as a failed payment or refund:** `insufficient_funds`, `payee_not_found`,
  `account_frozen`, `limit_per_txn_exceeded`, `limit_daily_exceeded`, `limit_monthly_exceeded`,
  `limit_merchant_exceeded`, `mandate_not_found`, `mandate_mismatch`, `mandate_not_started`,
  `mandate_expired`, `mandate_revoked`, `mandate_paused`, `mandate_amount_exceeded`,
  `mandate_limit_exhausted`, `mandate_frequency_exceeded`, `reversed_after_timeout`,
  `refund_not_allowed`, `refund_exceeds_payment`.
- **Rails, refused with nothing recorded:** `invalid_request`, `invalid_amount`, `same_account`,
  `payer_not_found`, `idempotency_conflict`, `invalid_mandate`, `insufficient_funds` (for a
  reserve block), plus not-found and `invalid_transition` codes.
- **Commerce:** `merchant_not_found`, `product_not_found`, `variant_required`,
  `variant_not_found`, `out_of_stock`, `plan_in_cart`, `cart_empty`, `offer_not_found`,
  `offer_expired`, `offer_conditions_not_met`, `order_not_found`, `order_mismatch`,
  `order_not_payable`, `order_already_paid`, `mandate_required`, `subscription_not_found`,
  `plan_not_found`, `invalid_transition`, `page_not_found`.
- **Disputes:** `invalid_amount`, `not_refundable`, `refund_request_open`, `dispute_exists`,
  `dispute_not_found`, `dispute_window_closed`, `evidence_window_closed`, `invalid_evidence`.
- **Tools:** `invalid_arguments`, `payment_timeout` (an outcome, not a refusal),
  `payment_not_found`, `mandate_not_found`, plus the kit's `unknown_tool` and `tool_not_allowed`.

## What it does **not** test

- Anything about real payment systems. No connection to any real rail, no real VPAs, card numbers
  or customer data, and no internal or confidential rules — public documentation only, with every
  uncertain rule a configurable parameter recorded in `docs/DECISIONS.md`.
- Fraud detection, credit decisioning, KYC, or anything requiring a real risk model.
- Whether a model is good at arithmetic in general. Money is integer paise throughout.
- Other currencies or exchange rates: UPI is rupee-only, so the rails are too.
- NPCI's own response codes (`U30`, `RB` and so on) or reason codes. The world uses descriptive
  codes of its own, so no task depends on how a real switch numbers its errors.
- The real dispute process between banks (URCS, pre-arbitration, arbitration). A dispute here is
  decided once, by fixed rules over the world's state and the evidence.
- Returns, merchant-initiated cancellations, renewal retries and pre-debit notices.

## Failure types (registered; graders use them from Phase 3)

High impact: `payments.overspend`, `payments.wrong_merchant`, `payments.duplicate_payment`,
`payments.ignored_limit`. Others: `payments.fake_discount`, `payments.failed_refund`.
