# World: payments

**Status:** rails built (Phase 1); commerce, disputes, tools, tasks and graders to come (Phases
2–3). **Version:** `payments-v0.1` (planned).
**Domain reviewer:** to be confirmed (payments operations / risk). No reviewer, no merge of a
grader.

## What it will simulate

UPI-inspired payment rails built from public concepts only: accounts and test-format handles,
mandates (recurring, one-time, reserve-style pre-authorised limits), per-transaction, daily,
monthly and per-merchant caps, payments with idempotency keys, refunds, and a double-entry ledger.
Alongside them: merchants with reliability profiles, catalogues, carts, subscriptions and invoices;
and after-payment operations — refund requests, disputes, reconciliation and a support inbox.

The failures are the point: insufficient funds, limit exceeded, expired or revoked mandates, payee
not found, timeouts where the payment actually succeeded, lagging status checks, and caps that
change mid-episode.

## Rails (built in Phase 1)

`services/rails/`: a FastAPI service over one SQLite database per episode. Tools (Phase 2) wrap
its internal API; graders read `/admin/*`. Every rule public sources leave open is a task setting,
listed in `docs/DECISIONS.md` (entries 17–36).

Failure codes, for the realism review:

- **Recorded as a failed payment or refund:** `insufficient_funds`, `payee_not_found`,
  `account_frozen`, `limit_per_txn_exceeded`, `limit_daily_exceeded`, `limit_monthly_exceeded`,
  `limit_merchant_exceeded`, `mandate_not_found`, `mandate_mismatch`, `mandate_not_started`,
  `mandate_expired`, `mandate_revoked`, `mandate_paused`, `mandate_amount_exceeded`,
  `mandate_limit_exhausted`, `mandate_frequency_exceeded`, `reversed_after_timeout`,
  `refund_not_allowed`, `refund_exceeds_payment`.
- **Refused with nothing recorded:** `invalid_request`, `invalid_amount`, `same_account`,
  `payer_not_found`, `idempotency_conflict`, plus not-found and `invalid_transition` codes for
  lookups and mandate actions.

Behaviours a task can switch on: scripted or seeded timeouts that settle later (by default to
success), lagging status checks, limit changes at a scheduled time, Reserve Pay blocks that hold
funds and expire.

## What it does **not** test

- Anything about real payment systems. No connection to any real rail, no real VPAs, card numbers
  or customer data, and no internal or confidential rules — public documentation only, with every
  uncertain rule a configurable parameter recorded in `docs/DECISIONS.md`.
- Fraud detection, credit decisioning, KYC, or anything requiring a real risk model.
- Whether a model is good at arithmetic in general. Money is integer paise throughout.
- Other currencies or exchange rates: UPI is rupee-only, so the rails are too.
- NPCI's own response codes (`U30`, `RB` and so on). The rails use descriptive codes of their
  own, so no task depends on how a real switch numbers its errors.

## Failure types (planned)

`payments.overspend`, `payments.wrong_merchant`, `payments.ignored_limit`,
`payments.fake_discount`, `payments.failed_refund`, `payments.duplicate_payment`.
