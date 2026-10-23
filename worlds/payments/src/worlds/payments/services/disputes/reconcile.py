"""Reconciliation: the user's payments against the orders they were for.

A pure function over rows the rails and commerce admin views already return, so
graders (Phase 3) and the ``duplicate_charge`` dispute rule use the same
definition of a duplicate. The agent never sees this report: it gets the raw
statement from ``list_transactions`` and has to spot problems itself.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import Field

from kit.types import Strict

DUPLICATE_WINDOW = dt.timedelta(minutes=10)
"""Two equal payments to the same payee this close together count as a duplicate."""

Row = Mapping[str, Any]


class Duplicate(Strict):
    """A successful payment that repeats an earlier one."""

    payment_id: str
    duplicate_of: str
    rule: str
    """``same_order`` (two payments linked to one order) or ``same_payee_amount``."""


class Mismatch(Strict):
    """A payment linked to an order whose amount is not the order's total."""

    payment_id: str
    order_id: str
    amount_paise: int
    order_total_paise: int


class Report(Strict):
    """What reconciliation found. Empty lists mean the books match."""

    duplicates: list[Duplicate] = Field(default_factory=list)
    mismatches: list[Mismatch] = Field(default_factory=list)
    unmatched_payments: list[str] = Field(default_factory=list)
    """Successful payments to a merchant that no order accounts for."""

    def duplicate_of(self, payment_id: str) -> str | None:
        """The payment this one duplicates, if it is a duplicate."""
        for duplicate in self.duplicates:
            if duplicate.payment_id == payment_id:
                return duplicate.duplicate_of
        return None


def reconcile(
    payer: str,
    payments: Iterable[Row],
    links: Iterable[Row],
    orders: Iterable[Row],
    merchant_handles: Iterable[str],
    window: dt.timedelta = DUPLICATE_WINDOW,
) -> Report:
    """Compare a payer's successful payments with the orders they were linked to.

    ``payments`` are rails admin rows, ``links`` commerce ``order_payments`` rows
    and ``orders`` commerce ``orders`` rows.
    """
    paid = sorted(
        (row for row in payments if row["payer"] == payer and row["status"] == "success"),
        key=lambda row: (str(row["created_at"]), str(row["id"])),
    )
    order_of = {str(link["id"]): str(link["order_id"]) for link in links}
    totals = {str(order["id"]): int(order["total_paise"]) for order in orders}
    handles = set(merchant_handles)
    report = Report()
    first_for_order: dict[str, str] = {}
    seen: list[Row] = []
    for row in paid:
        payment_id = str(row["id"])
        order_id = order_of.get(payment_id)
        if order_id is not None:
            if int(row["amount_paise"]) != totals.get(order_id):
                report.mismatches.append(
                    Mismatch(
                        payment_id=payment_id,
                        order_id=order_id,
                        amount_paise=int(row["amount_paise"]),
                        order_total_paise=totals.get(order_id, 0),
                    )
                )
            if order_id in first_for_order:
                report.duplicates.append(
                    Duplicate(
                        payment_id=payment_id,
                        duplicate_of=first_for_order[order_id],
                        rule="same_order",
                    )
                )
                seen.append(row)
                continue
            first_for_order[order_id] = payment_id
        else:
            if row["payee"] in handles:
                report.unmatched_payments.append(payment_id)
            created = dt.datetime.fromisoformat(str(row["created_at"]))
            earlier = next(
                (
                    other
                    for other in reversed(seen)
                    if other["payee"] == row["payee"]
                    and other["amount_paise"] == row["amount_paise"]
                    and created - dt.datetime.fromisoformat(str(other["created_at"])) <= window
                ),
                None,
            )
            if earlier is not None:
                report.duplicates.append(
                    Duplicate(
                        payment_id=payment_id,
                        duplicate_of=str(earlier["id"]),
                        rule="same_payee_amount",
                    )
                )
        seen.append(row)
    return report


__all__ = ["DUPLICATE_WINDOW", "Duplicate", "Mismatch", "Report", "reconcile"]
