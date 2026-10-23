"""The only code that carries information between the three services.

Services never call each other (see docs/DECISIONS.md). When something in one
service matters to another, the episode or a tool passes it on here:

- a payment made for an order is linked to the order in commerce;
- a timed-out payment that settles is reported to commerce, as a payment
  webhook would be;
- a subscription renewal is collected through the rails with the user's mandate;
- a refund a merchant agrees to, or a dispute that is won, is paid through the
  rails and reported back to disputes;
- notices (invoices, receipts, shipping) are delivered to the inbox;
- rules that need another service's state are handed the facts when they run.

Reads here use the services' admin views for the true state, because this is
the world's own plumbing, not something the agent can see.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.trace import DiffEntry

from .services.commerce import CommerceService
from .services.common import Row, diff_of, parse
from .services.disputes import DisputesService, Report, reconcile
from .services.disputes.core import FACT_EVENTS
from .services.rails import RailsService
from .services.rails.core import EVENT_SETTLE


class Links:
    """The user's world: three services and the handle the agent acts for."""

    def __init__(
        self,
        rails: RailsService,
        commerce: CommerceService,
        disputes: DisputesService,
        user: str,
    ) -> None:
        self.rails = rails
        self.commerce = commerce
        self.disputes = disputes
        self.user = user

    @classmethod
    def from_services(cls, services: Mapping[str, Any], user: str) -> Links:
        """Build from an episode's ``services`` mapping, as a tool handler receives it."""
        rails, commerce, disputes = services["rails"], services["commerce"], services["disputes"]
        assert isinstance(rails, RailsService)
        assert isinstance(commerce, CommerceService)
        assert isinstance(disputes, DisputesService)
        return cls(rails, commerce, disputes, user)

    # --------------------------------------------------------------- reading
    def true_payment(self, *, payment_id: str | None = None, key: str | None = None) -> Row:
        """A payment as it really is, never lagged."""
        filters = {"id": payment_id} if payment_id is not None else {"idempotency_key": key}
        rows = self.rails.query("payments", **filters)
        if not rows:
            msg = f"no payment {payment_id or key!r}"
            raise ToolRejected("payment_not_found", msg)
        row: Row = rows[0]
        return row

    def refundable(self, payment: Row) -> int:
        """What is left to refund on a payment: nothing unless it succeeded."""
        if payment["status"] != "success":
            return 0
        refunded = sum(
            int(row["amount_paise"])
            for row in self.rails.query("refunds", payment_id=str(payment["id"]), status="success")
        )
        return int(payment["amount_paise"]) - refunded

    def reconciliation(self) -> Report:
        """The user's payments reconciled against commerce's orders."""
        merchants = self.commerce.query("merchants")
        return reconcile(
            self.user,
            self.rails.query("payments"),
            self.commerce.query("order_payments"),
            self.commerce.query("orders"),
            [str(row["handle"]) for row in merchants],
        )

    def facts(self, payment_id: str, order_id: str | None) -> dict[str, Any]:
        """What a refund or dispute rule needs to know about the world right now."""
        payment = self.true_payment(payment_id=payment_id)
        facts: dict[str, Any] = {
            "refundable_paise": self.refundable(payment),
            "duplicate_of": self.reconciliation().duplicate_of(payment_id),
            "order_status": None,
            "not_received": False,
        }
        if order_id is not None:
            order = self.commerce.call("GET", f"/orders/{order_id}")
            facts["order_status"] = order["status"]
            expected = order["expected_delivery_at"]
            facts["not_received"] = bool(
                order["status"] in ("paid", "shipped")
                and expected is not None
                and self.rails.clock.now() >= parse(str(expected))
            )
        return facts

    # --------------------------------------------------------------- writing
    def notify(self, notices: Iterable[Row]) -> list[DiffEntry]:
        """Deliver notices from commerce to the user's inbox."""
        batch = [dict(item) for item in notices]
        if not batch:
            return []
        return diff_of(self.disputes.call("POST", "/internal/notices", {"notices": batch}))

    def link_order_payment(self, order_id: str, key: str) -> list[DiffEntry]:
        """Tell commerce about a payment made for one of its orders."""
        payment = self.true_payment(key=key)
        body = self.commerce.call(
            "POST",
            f"/orders/{order_id}/payments",
            {
                "payment_id": payment["id"],
                "amount_paise": payment["amount_paise"],
                "status": payment["status"],
                "mandate_id": payment["mandate_id"],
            },
        )
        return diff_of(body) + self.notify(body["notices"])

    def settled(self, payment_id: str) -> list[DiffEntry]:
        """A timed-out payment finished: the merchant's order learns how."""
        payment = self.true_payment(payment_id=payment_id)
        body = self.commerce.call(
            "POST",
            "/internal/payment-status",
            {"payment_id": payment_id, "status": payment["status"]},
        )
        return diff_of(body) + self.notify(body["notices"])

    def collect(self, charge: Row) -> list[DiffEntry]:
        """Collect a subscription renewal through the user's mandate."""
        body = self.rails.call(
            "POST",
            "/payments",
            {
                "idempotency_key": charge["idempotency_key"],
                "payer": charge["payer"],
                "payee": charge["payee"],
                "amount_paise": charge["amount_paise"],
                "mandate_id": charge["mandate_id"],
                "note": charge["note"],
            },
        )
        return diff_of(body) + self.link_order_payment(
            str(charge["order_id"]), str(charge["idempotency_key"])
        )

    def pay_back(self, due: Row) -> list[DiffEntry]:
        """Pay a refund a merchant agreed to, or a chargeback, and tell disputes."""
        body = self.rails.call(
            "POST",
            "/refunds",
            {
                "idempotency_key": due["idempotency_key"],
                "payment_id": due["payment_id"],
                "amount_paise": due["amount_paise"],
                "reason": due["reason"],
            },
        )
        refund = body["refund"]
        outcome = self.disputes.call(
            "POST",
            "/internal/refund-outcome",
            {
                "kind": due["kind"],
                "id": due["id"],
                "refund_id": refund["id"],
                "status": refund["status"],
                "amount_paise": refund["amount_paise"],
            },
        )
        return diff_of(body) + diff_of(outcome)

    # ---------------------------------------------------------------- events
    def on_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        """Deliver one due event to its service and carry its effects to the others."""
        if self.rails.handles(event):
            diff = self.rails.apply_event(event)
            if event.name == EVENT_SETTLE:
                diff += self.settled(str(event.payload["payment_id"]))
            return diff
        if self.commerce.handles(event):
            body = self.commerce.deliver(event)
            diff = diff_of(body) + self.notify(body["notices"])
            if body.get("charge") is not None:
                diff += self.collect(body["charge"])
            return diff
        if self.disputes.handles(event):
            extra: dict[str, Any] = {}
            if event.name in FACT_EVENTS:
                subject = self.disputes.call(
                    "GET",
                    "/internal/event-subject",
                    params={"name": event.name, **{k: str(v) for k, v in event.payload.items()}},
                )
                if subject is not None:
                    extra["facts"] = self.facts(str(subject["payment_id"]), subject["order_id"])
            body = self.disputes.deliver(event, extra)
            diff = diff_of(body)
            if body.get("refund_due") is not None:
                diff += self.pay_back(body["refund_due"])
            return diff
        msg = f"no service handles event {event.name!r}"
        raise RuntimeError(msg)


__all__ = ["Links"]
