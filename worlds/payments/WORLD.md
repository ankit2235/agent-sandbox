# World: payments

**Status:** not built yet. Phases 1–3 of the build plan. **Version:** `payments-v0.1` (planned).
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

## What it does **not** test

- Anything about real payment systems. No connection to any real rail, no real VPAs, card numbers
  or customer data, and no internal or confidential rules — public documentation only, with every
  uncertain rule a configurable parameter recorded in `docs/DECISIONS.md`.
- Fraud detection, credit decisioning, KYC, or anything requiring a real risk model.
- Whether a model is good at arithmetic in general. Money is integer paise throughout.

## Failure types (planned)

`payments.overspend`, `payments.wrong_merchant`, `payments.ignored_limit`,
`payments.fake_discount`, `payments.failed_refund`, `payments.duplicate_payment`.
