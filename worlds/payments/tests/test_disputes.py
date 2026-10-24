"""Refund requests, disputes and reconciliation."""

from typing import Any

import pytest
from payments_testkit import DAY, USER, Session, base_state, handle, make_task

from worlds.payments.services.disputes import reconcile

HONEST, VAGUE, MIXUP = "honest-goods", "vague-goods", "mixup-goods"


def state_with(merchant_id: str, disputes: dict[str, Any] | None = None, **behaviour: Any) -> dict:
    state = base_state()
    for merchant in state["commerce"]["merchants"]:
        if merchant["id"] == merchant_id:
            merchant["behaviour"] = behaviour
    if disputes is not None:
        state["disputes"] = disputes
    return state


def wrong_item_order(merchant_id: str = MIXUP, **extra: Any) -> tuple[Session, str]:
    s = Session(make_task(state_with(merchant_id, wrong_item_on_orders=[1], **extra)))
    order_id = s.buy(merchant_id)
    s.ok("wait", hours=72)
    assert s.order(order_id)["status"] == "wrong_item"
    return s, order_id


def payment_of(s: Session, order_id: str) -> str:
    return str(s.order(order_id)["payment_id"])


# ---------------------------------------------------------- refund requests
def test_a_valid_refund_request_is_paid_through_the_rails() -> None:
    s, order_id = wrong_item_order()
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="wrong item")
    s.ok("wait", hours=24)
    request = s.admin("disputes", "refund_requests")[0]
    assert request["status"] == "accepted" and request["refund_id"]
    refund = s.admin("rails", "refunds")[0]
    assert (refund["id"], refund["amount_paise"]) == (request["refund_id"], 149_900)
    assert refund["idempotency_key"] == f"refund-{request['id']}"


def test_a_refund_for_an_order_delivered_as_ordered_is_declined() -> None:
    s = Session()
    order_id = s.buy(HONEST)
    s.ok("wait", hours=72)
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="changed my mind")
    s.ok("wait", hours=24)
    assert s.admin("disputes", "refund_requests")[0]["status"] == "declined"
    assert s.admin("rails", "refunds") == []


def test_an_ignoring_merchant_lets_the_request_expire() -> None:
    s, order_id = wrong_item_order(VAGUE, refund_response="ignore")
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="wrong item")
    s.ok("wait", hours=4 * 24)
    assert s.admin("disputes", "refund_requests")[0]["status"] == "open"
    s.ok("wait", hours=24)
    assert s.admin("disputes", "refund_requests")[0]["status"] == "expired"
    assert any("expired" in m["subject"] for m in s.admin("disputes", "messages"))


def test_refund_request_rules() -> None:
    s, order_id = wrong_item_order()
    assert s.refused("request_refund", order_id=order_id, amount_paise=149_901,
                     reason="x") == "not_refundable"  # fmt: skip
    assert s.refused("request_refund", order_id=order_id, amount_paise=0, reason="x") == (
        "invalid_amount"
    )
    s.ok("request_refund", order_id=order_id, amount_paise=100, reason="x")
    assert s.refused("request_refund", order_id=order_id, amount_paise=100, reason="x") == (
        "refund_request_open"
    )
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    unpaid = s.ok("checkout", merchant_id=HONEST)["order"]["id"]
    assert s.refused("request_refund", order_id=unpaid, amount_paise=100, reason="x") == (
        "not_refundable"
    )


def test_a_merchant_that_cannot_cover_a_refund_records_a_failure() -> None:
    s, order_id = wrong_item_order()
    s.episode.rails.call("POST", "/payments", {  # the merchant spends its money first
        "idempotency_key": "drain", "payer": handle(MIXUP), "payee": handle(HONEST),
        "amount_paise": s.balance(handle(MIXUP)),
    })  # fmt: skip
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="wrong item")
    s.ok("wait", hours=24)
    assert s.admin("disputes", "refund_requests")[0]["status"] == "refund_failed"
    assert s.admin("rails", "refunds")[0]["failure_code"] == "insufficient_funds"


# ----------------------------------------------------------------- disputes
def open_wrong_item_dispute(s: Session, order_id: str, *, evidence: bool = True) -> str:
    dispute = s.ok("open_dispute", payment_id=payment_of(s, order_id), reason="wrong_item",
                   description="Wrong item.")["dispute"]  # fmt: skip
    if evidence:
        s.ok("submit_evidence", dispute_id=dispute["id"], kind="order", reference_id=order_id)
    return str(dispute["id"])


def test_a_wrong_item_dispute_needs_the_merchant_asked_first() -> None:
    s, order_id = wrong_item_order(refund_response="ignore")
    open_wrong_item_dispute(s, order_id)
    s.ok("wait", hours=15 * 24)
    dispute = s.admin("disputes", "disputes")[0]
    assert dispute["status"] == "lost"
    assert dispute["decision"] == "the merchant was not asked for a refund first"


def test_a_wrong_item_dispute_without_evidence_is_lost() -> None:
    s, order_id = wrong_item_order(refund_response="ignore")
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="wrong item")
    s.ok("wait", hours=5 * 24)
    open_wrong_item_dispute(s, order_id, evidence=False)
    s.ok("wait", hours=15 * 24)
    assert s.admin("disputes", "disputes")[0]["decision"] == "no evidence pointing to the order"
    assert s.balance(USER) == 5_000_000 - 149_900


def test_a_wrong_item_dispute_with_contact_and_evidence_is_won() -> None:
    s, order_id = wrong_item_order(refund_response="ignore")
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="wrong item")
    s.ok("wait", hours=5 * 24)
    dispute_id = open_wrong_item_dispute(s, order_id)
    s.ok("wait", hours=15 * 24 - 1)
    assert s.admin("disputes", "disputes")[0]["status"] == "open"  # decided at the deadline
    s.ok("wait", hours=1)
    dispute = s.admin("disputes", "disputes")[0]
    assert (dispute["status"], dispute["awarded_paise"]) == ("won", 149_900)
    refund = s.admin("rails", "refunds")[0]
    assert refund["idempotency_key"] == f"chargeback-{dispute_id}"
    assert s.balance(USER) == 5_000_000


def test_a_dispute_on_an_order_delivered_as_ordered_is_lost() -> None:
    s = Session()
    order_id = s.buy(HONEST)
    s.ok("wait", hours=72)
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="wrong item")
    open_wrong_item_dispute(s, order_id)
    s.ok("wait", hours=15 * 24)
    assert s.admin("disputes", "disputes")[0]["decision"] == (
        "the order was not delivered with the wrong item"
    )


def test_a_merchant_that_never_answers_a_dispute_is_deemed_to_accept() -> None:
    s, order_id = wrong_item_order(contests_disputes=False, refund_response="ignore")
    open_wrong_item_dispute(s, order_id, evidence=False)  # no contact, no evidence
    s.ok("wait", hours=15 * 24)
    dispute = s.admin("disputes", "disputes")[0]
    assert dispute["status"] == "won"
    assert dispute["decision"].startswith("deemed accepted")


def test_not_received_is_judged_on_the_promised_date() -> None:
    state = state_with("slow-goods", deliver_after_seconds=60 * DAY, ship_after_seconds=DAY)
    s = Session(make_task(state))
    order_id = s.buy("slow-goods")
    s.ok("wait", hours=4 * 24)
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="where is it")
    s.ok("wait", hours=5 * 24)  # the slow merchant answers on day 4: by then it is late
    assert s.admin("disputes", "refund_requests")[0]["status"] == "accepted"
    assert s.balance(USER) == 5_000_000


def test_a_not_received_dispute_is_won_while_the_order_is_still_missing() -> None:
    state = state_with("slow-goods", deliver_after_seconds=60 * DAY, refund_response="ignore")
    s = Session(make_task(state))
    order_id = s.buy("slow-goods")
    s.ok("wait", hours=6 * 24)
    s.ok("request_refund", order_id=order_id, amount_paise=149_900, reason="not arrived")
    s.ok("wait", hours=5 * 24)
    dispute = s.ok("open_dispute", payment_id=payment_of(s, order_id), reason="not_received",
                   description="Never arrived.")["dispute"]  # fmt: skip
    s.ok("submit_evidence", dispute_id=dispute["id"], kind="order", reference_id=order_id)
    s.ok("wait", hours=15 * 24)
    assert s.admin("disputes", "disputes")[0]["status"] == "won"


def test_a_duplicate_charge_dispute_needs_the_original_as_evidence() -> None:
    state = base_state()
    state["rails"]["faults"] = {"timeout_on_attempts": [1]}
    s = Session(make_task(state))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    pay = {"payee": request["payee"], "amount_paise": request["amount_paise"],
           "order_id": request["order_id"]}  # fmt: skip
    s.ok("pay", idempotency_key="a", **pay)
    s.ok("pay", idempotency_key="b", **pay)
    s.ok("wait", hours=1)
    first = s.admin("rails", "payments", idempotency_key="a")[0]["id"]
    second = s.admin("rails", "payments", idempotency_key="b")[0]["id"]
    dispute = s.ok("open_dispute", payment_id=second, reason="duplicate_charge",
                   description="Charged twice for one order.")["dispute"]  # fmt: skip
    assert s.refused("submit_evidence", dispute_id=dispute["id"], kind="payment",
                     reference_id="pay_nope") == "invalid_evidence"  # fmt: skip
    s.ok("submit_evidence", dispute_id=dispute["id"], kind="payment", reference_id=first)
    s.ok("wait", hours=15 * 24)
    assert s.admin("disputes", "disputes")[0]["status"] == "won"
    assert s.balance(USER) == 5_000_000 - 149_900


def test_a_duplicate_charge_dispute_on_the_original_is_lost() -> None:
    s = Session()
    order_id = s.buy(HONEST)
    dispute = s.ok("open_dispute", payment_id=payment_of(s, order_id),
                   reason="duplicate_charge", description="x")["dispute"]  # fmt: skip
    s.ok("wait", hours=15 * 24)
    assert s.admin("disputes", "disputes")[0]["decision"] == (
        "the payment is not a duplicate of another payment"
    )
    assert dispute["status"] == "open"


def test_dispute_rules() -> None:
    s = Session(make_task(state_with(HONEST, disputes={"settings": {
        "dispute_window_seconds": 2 * DAY}})))  # fmt: skip
    order_id = s.buy(HONEST)
    payment_id = payment_of(s, order_id)
    assert s.refused("open_dispute", payment_id="pay_nope", reason="wrong_item",
                     description="x") == "payment_not_found"  # fmt: skip
    assert s.refused("open_dispute", payment_id=payment_id, reason="wrong_item",
                     description="x", amount_paise=149_901) == "not_refundable"  # fmt: skip
    s.ok("open_dispute", payment_id=payment_id, reason="wrong_item", description="x")
    assert s.refused("open_dispute", payment_id=payment_id, reason="not_received",
                     description="x") == "dispute_exists"  # fmt: skip
    assert s.refused("submit_evidence", dispute_id="dsp_nope", kind="order",
                     reference_id=order_id) == "dispute_not_found"  # fmt: skip
    late = s.buy(HONEST)
    s.ok("wait", hours=3 * 24)
    assert s.refused("open_dispute", payment_id=payment_of(s, late), reason="wrong_item",
                     description="x") == "dispute_window_closed"  # fmt: skip


def test_a_dispute_on_a_direct_payment_finds_the_merchant_by_handle() -> None:
    s = Session()
    paid = s.ok("pay", payee=handle(HONEST), amount_paise=5_000, idempotency_key="direct")
    dispute = s.ok("open_dispute", payment_id=paid["payment"]["id"], reason="duplicate_charge",
                   description="x")["dispute"]  # fmt: skip
    assert (dispute["merchant_id"], dispute["order_id"]) == (HONEST, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [("dispute_window_seconds", 45 * DAY), ("dispute_response_seconds", 15 * DAY),
     ("refund_response_seconds", 5 * DAY)],
)  # fmt: skip
def test_default_windows(field: str, value: int) -> None:
    from worlds.payments.services.disputes.models import DisputeSettings

    assert getattr(DisputeSettings(), field) == value


# ------------------------------------------------------------ reconciliation
def pay_row(pid: str, at: str, amount: int = 100, payee: str = "shop@sandbox") -> dict:
    return {"id": pid, "payer": USER, "payee": payee, "amount_paise": amount,
            "status": "success", "created_at": f"2026-01-01T09:{at}:00.000000Z"}  # fmt: skip


def test_reconcile_finds_duplicates_mismatches_and_strays() -> None:
    payments = [
        pay_row("p1", "00"), pay_row("p2", "01"),  # both for order o1: p2 duplicates p1
        pay_row("p3", "02", amount=90),  # for o2, which costs 100
        pay_row("p4", "03", payee="cafe@sandbox"), pay_row("p5", "05", payee="cafe@sandbox"),
        pay_row("p6", "30", payee="cafe@sandbox"),  # outside the 10-minute window
        {**pay_row("p7", "04"), "status": "failed"},
        {**pay_row("p8", "04"), "payer": "someone@sandbox"},
    ]  # fmt: skip
    links = [{"id": "p1", "order_id": "o1"}, {"id": "p2", "order_id": "o1"},
             {"id": "p3", "order_id": "o2"}]  # fmt: skip
    orders = [{"id": "o1", "total_paise": 100}, {"id": "o2", "total_paise": 100}]
    report = reconcile(USER, payments, links, orders, ["shop@sandbox", "cafe@sandbox"])
    assert [(d.payment_id, d.duplicate_of, d.rule) for d in report.duplicates] == [
        ("p2", "p1", "same_order"), ("p5", "p4", "same_payee_amount"),
    ]  # fmt: skip
    assert [(m.payment_id, m.amount_paise, m.order_total_paise) for m in report.mismatches] == [
        ("p3", 90, 100)
    ]
    assert report.unmatched_payments == ["p4", "p5", "p6"]


def test_reconcile_of_clean_books_is_empty() -> None:
    payments = [pay_row("p1", "00")]
    report = reconcile(USER, payments, [{"id": "p1", "order_id": "o1"}],
                       [{"id": "o1", "total_paise": 100}], ["shop@sandbox"])  # fmt: skip
    assert report.duplicates == report.mismatches == report.unmatched_payments == []
