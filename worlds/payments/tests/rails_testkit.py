"""Shared helpers for the rails tests. A plain module, so tests can import it by name."""

import itertools
from typing import Any

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from worlds.payments.services.rails import (
    PaymentRequest,
    Rails,
    RailsConfig,
    RefundRequest,
    Result,
)
from worlds.payments.services.rails.core import CLEARING, HOLDS, ISSUANCE

ALICE, BOB, SHOP = "alice@sandbox", "bob@sandbox", "shop@sandbox"


def config_dict(**overrides: Any) -> dict[str, Any]:
    """A small world: two people and a shop. Overrides replace whole top-level keys."""
    base: dict[str, Any] = {
        "accounts": [
            {"handle": ALICE, "holder": "Alice Example", "balance_paise": 500_000},
            {"handle": BOB, "holder": "Bob Example", "balance_paise": 100_000},
            {"handle": SHOP, "holder": "Example Shop", "kind": "merchant"},
        ],
    }
    base.update(overrides)
    return base


class Harness:
    """The rails core plus a clock that delivers the events the rails schedule."""

    def __init__(self, config: dict[str, Any] | None = None, seed: int = 7) -> None:
        self.clock = SimClock(EPOCH)
        self.rails = Rails(SeededRNG(seed).child("rails"))
        self._keys = itertools.count(1)
        self.config = RailsConfig.model_validate(config if config is not None else config_dict())
        self.schedule(self.rails.load(self.config, self.clock.now()))

    @property
    def now(self):
        return self.clock.now()

    def schedule(self, result: Result) -> Result:
        for event in result.scheduled:
            self.clock.schedule(event)
        return result

    def pay(
        self,
        amount: int = 10_000,
        payer: str = ALICE,
        payee: str = SHOP,
        key: str | None = None,
        mandate_id: str | None = None,
        note: str = "",
    ) -> Result:
        request = PaymentRequest(
            idempotency_key=key or f"pay-{next(self._keys)}",
            payer=payer,
            payee=payee,
            amount_paise=amount,
            mandate_id=mandate_id,
            note=note,
        )
        return self.schedule(self.rails.create_payment(request, self.now))

    def refund(
        self, payment_id: str, amount: int, key: str | None = None, reason: str = "returned"
    ) -> Result:
        request = RefundRequest(
            idempotency_key=key or f"refund-{next(self._keys)}",
            payment_id=payment_id,
            amount_paise=amount,
            reason=reason,
        )
        return self.rails.create_refund(request, self.now)

    def advance(self, seconds: float) -> list[Result]:
        self.clock.advance(seconds)
        return [
            self.schedule(self.rails.apply_event(event.name, event.payload, self.now))
            for event in self.clock.due()
        ]

    def admin(self, resource: str, **filters: str) -> Any:
        return self.rails.admin(resource, self.now, filters)

    def balance(self, account: str) -> int:
        """Balance by handle, or by system account id."""
        for row in self.admin("accounts"):
            if account in (row["handle"], row["id"]):
                return int(row["balance_paise"])
        raise KeyError(account)

    def stored(self, payment_id: str) -> dict[str, Any]:
        """The true payment row, as graders see it."""
        return next(row for row in self.admin("payments") if row["id"] == payment_id)


def check_books(rails: Rails, now, opening_total: int) -> None:
    """Every ledger invariant the rails promise, checked from the admin view."""
    accounts = rails.admin("accounts", now, {})
    ledger = rails.admin("ledger", now, {})
    payments = rails.admin("payments", now, {})
    mandates = rails.admin("mandates", now, {})
    refunds = rails.admin("refunds", now, {})

    balances = {row["id"]: row["balance_paise"] for row in accounts}
    assert sum(balances.values()) == 0, "issuance must offset every other balance exactly"
    assert sum(v for k, v in balances.items() if k != ISSUANCE) == opening_total

    by_account: dict[str, int] = {}
    by_txn: dict[str, int] = {}
    for entry in ledger:
        by_account[entry["account_id"]] = (
            by_account.get(entry["account_id"], 0) + entry["delta_paise"]
        )
        by_txn[entry["txn_ref"]] = by_txn.get(entry["txn_ref"], 0) + entry["delta_paise"]
    for account, balance in balances.items():
        assert balance == by_account.get(account, 0), f"{account} disagrees with its ledger"
        assert account == ISSUANCE or balance >= 0
    assert all(total == 0 for total in by_txn.values()), "every transaction must balance"

    in_flight = sum(p["amount_paise"] for p in payments if p["status"] == "pending")
    assert balances[CLEARING] == in_flight
    held = sum(m["remaining_limit_paise"] for m in mandates if m["type"] == "reserve")
    assert balances[HOLDS] == held

    refunded: dict[str, int] = {}
    for refund in refunds:
        if refund["status"] == "success":
            refunded[refund["payment_id"]] = (
                refunded.get(refund["payment_id"], 0) + refund["amount_paise"]
            )
    amounts = {p["id"]: p["amount_paise"] for p in payments}
    assert all(total <= amounts[pid] for pid, total in refunded.items())
