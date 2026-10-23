"""Commerce through the tools: carts, offers, checkout, orders, pages and subscriptions."""

import datetime as dt

import pytest
from payments_testkit import USER, Session, base_state, handle, make_task

from worlds.payments.services.commerce.core import next_period, raised
from worlds.payments.services.commerce.pages import money
from worlds.payments.services.rails.models import Frequency

HONEST = "honest-goods"


def test_money_uses_indian_grouping() -> None:
    assert money(0) == "₹0.00"
    assert money(149_900) == "₹1,499.00"
    assert money(123_456_789) == "₹12,34,567.89"
    assert money(-5) == "-₹0.05"


def test_a_price_rise_rounds_half_up_to_the_paisa() -> None:
    assert raised(149_900, 1_500) == 172_385
    assert raised(1, 5_000) == 2  # 1.5 rounds up


@pytest.mark.parametrize(
    ("start", "frequency", "expected"),
    [
        ("2026-01-31T09:00:00+00:00", Frequency.MONTHLY, "2026-02-28T09:00:00+00:00"),
        ("2026-12-15T09:00:00+00:00", Frequency.MONTHLY, "2027-01-15T09:00:00+00:00"),
        ("2026-01-01T09:00:00+00:00", Frequency.WEEKLY, "2026-01-08T09:00:00+00:00"),
        ("2026-01-01T09:00:00+00:00", Frequency.DAILY, "2026-01-02T09:00:00+00:00"),
    ],
)
def test_billing_periods(start: str, frequency: Frequency, expected: str) -> None:
    assert next_period(dt.datetime.fromisoformat(start), frequency).isoformat() == expected


# -------------------------------------------------------------------- carts
def test_cart_lines_check_variants_and_stock() -> None:
    s = Session()
    tee = {"merchant_id": HONEST, "product_id": f"{HONEST}-tee"}
    assert s.refused("add_to_cart", quantity=1, **tee) == "variant_required"
    assert s.refused("add_to_cart", quantity=1, variant="XL", **tee) == "variant_not_found"
    assert s.refused("add_to_cart", quantity=6, variant="M", **tee) == "out_of_stock"
    assert s.refused("add_to_cart", merchant_id=HONEST, product_id="nope", quantity=1) == (
        "product_not_found"
    )
    assert s.refused("add_to_cart", merchant_id="nobody", product_id="x", quantity=1) == (
        "merchant_not_found"
    )
    s.ok("add_to_cart", quantity=2, variant="M", **tee)
    s.ok("add_to_cart", quantity=1, variant="L", **tee)
    assert s.ok("view_cart", merchant_id=HONEST)["cart"]["subtotal_paise"] == 3 * 59_900
    s.ok("add_to_cart", quantity=0, variant="M", **tee)
    assert s.ok("view_cart", merchant_id=HONEST)["cart"]["subtotal_paise"] == 59_900


def test_a_plan_is_checked_out_on_its_own() -> None:
    s = Session()
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    assert s.refused("add_to_cart", merchant_id=HONEST, product_id="honest-basic",
                     quantity=1) == "plan_in_cart"  # fmt: skip
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=0)
    assert s.refused("add_to_cart", merchant_id=HONEST, product_id="honest-basic",
                     quantity=2) == "invalid_request"  # fmt: skip
    s.ok("add_to_cart", merchant_id=HONEST, product_id="honest-basic", quantity=1)
    assert s.refused("apply_offer", merchant_id=HONEST, code="SAVE10") == (
        "offer_conditions_not_met"
    )


def test_checkout_needs_something_in_the_cart() -> None:
    assert Session().refused("checkout", merchant_id=HONEST) == "cart_empty"


# ------------------------------------------------------------------- offers
def test_offers_valid_expired_and_conditional() -> None:
    s = Session()
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    assert s.refused("apply_offer", merchant_id=HONEST, code="OLD50") == "offer_expired"
    assert s.refused("apply_offer", merchant_id=HONEST, code="BIG200") == (
        "offer_conditions_not_met"
    )
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=2)
    cart = s.ok("apply_offer", merchant_id=HONEST, code="big200")["cart"]  # codes ignore case
    assert (cart["offer_code"], cart["total_paise"]) == ("BIG200", 299_800 - 20_000)


def test_an_offer_that_stops_applying_is_dropped_at_checkout() -> None:
    s = Session()
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=2)
    s.ok("apply_offer", merchant_id=HONEST, code="BIG200")
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    cart = s.ok("view_cart", merchant_id=HONEST)["cart"]
    assert cart["offer_problem"].startswith("offer_conditions_not_met")
    assert cart["total_paise"] == 149_900
    order = s.ok("checkout", merchant_id=HONEST)["order"]
    assert (order["offer_code"], order["discount_paise"]) == (None, 0)
    assert "no longer applies" in s.results[-1].observation.text


def test_first_order_only_and_product_specific_offers() -> None:
    state = base_state()
    state["commerce"]["offers"] += [
        {"code": "WELCOME", "merchant_id": HONEST, "flat_off_paise": 10_000,
         "first_order_only": True},
        {"code": "TEEHALF", "merchant_id": HONEST, "percent_off": 50,
         "product_ids": [f"{HONEST}-tee"], "max_discount_paise": 20_000},
    ]  # fmt: skip
    s = Session(make_task(state))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    assert s.refused("apply_offer", merchant_id=HONEST, code="TEEHALF") == (
        "offer_conditions_not_met"
    )
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-tee", variant="S", quantity=1)
    assert (
        s.ok("apply_offer", merchant_id=HONEST, code="TEEHALF")["cart"]["discount_paise"] == 20_000
    )
    s.ok("apply_offer", merchant_id=HONEST, code="WELCOME")
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    s.ok("pay", payee=request["payee"], amount_paise=request["amount_paise"],
         idempotency_key="k", order_id=request["order_id"])  # fmt: skip
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    assert s.refused("apply_offer", merchant_id=HONEST, code="WELCOME") == (
        "offer_conditions_not_met"
    )


def test_a_full_discount_needs_no_payment() -> None:
    state = base_state()
    state["commerce"]["offers"].append({"code": "FREE", "merchant_id": HONEST, "percent_off": 100})
    s = Session(make_task(state))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    s.ok("apply_offer", merchant_id=HONEST, code="FREE")
    data = s.ok("checkout", merchant_id=HONEST)
    assert data["payment_request"] is None and data["order"]["status"] == "paid"
    s.ok("wait", hours=72)
    assert s.order(data["order"]["id"])["status"] == "delivered"
    assert s.balance(USER) == 5_000_000


# ------------------------------------------------------------------- orders
def test_an_unpaid_order_lapses_and_gives_its_stock_back() -> None:
    s = Session()
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=3)
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    stock = {p["id"]: p["stock"] for p in s.admin("commerce", "products")}
    assert stock[f"{HONEST}-kettle"] == 2
    s.ok("wait", hours=0.25)
    assert s.order(request["order_id"])["status"] == "cancelled"
    stock = {p["id"]: p["stock"] for p in s.admin("commerce", "products")}
    assert stock[f"{HONEST}-kettle"] == 5
    assert (
        s.refused(
            "pay",
            payee=request["payee"],
            amount_paise=request["amount_paise"],
            idempotency_key="late",
            order_id=request["order_id"],
        )
        == "order_not_payable"
    )


def test_the_payment_window_is_a_setting() -> None:
    state = base_state()
    state["commerce"]["settings"] = {"order_payment_window_seconds": 60}
    s = Session(make_task(state))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    order = s.ok("checkout", merchant_id=HONEST)["order"]
    assert order["pay_by"] == "2026-01-01T09:01:02.000000Z"


def test_a_pending_payment_keeps_the_order_open_past_its_window() -> None:
    state = base_state()
    state["rails"]["faults"] = {"timeout_on_attempts": [1], "settle_after_seconds": 1_200}
    s = Session(make_task(state))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    s.ok("pay", payee=request["payee"], amount_paise=request["amount_paise"],
         idempotency_key="k", order_id=request["order_id"])  # fmt: skip
    s.ok("wait", hours=0.25)
    assert s.order(request["order_id"])["status"] == "created"  # still in flight
    s.ok("wait", hours=0.25)
    assert s.order(request["order_id"])["status"] == "paid"  # settled after 20 minutes


def test_a_reversed_payment_past_the_window_cancels_the_order() -> None:
    state = base_state()
    state["rails"]["faults"] = {
        "timeout_on_attempts": [1], "settle_after_seconds": 1_200, "timeout_outcome": "reversed",
    }  # fmt: skip
    s = Session(make_task(state))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    s.ok("pay", payee=request["payee"], amount_paise=request["amount_paise"],
         idempotency_key="k", order_id=request["order_id"])  # fmt: skip
    s.ok("wait", hours=1)
    order = s.order(request["order_id"])
    assert (order["status"], order["cancel_reason"]) == ("cancelled", "payment_window_closed")
    assert s.balance(USER) == 5_000_000


def test_invoices_and_receipts_are_issued_and_delivered() -> None:
    s = Session()
    order_id = s.buy(HONEST)
    order = s.ok("get_order", order_id=order_id)["order"]
    kinds = [d["kind"] for d in order["documents"]]
    assert kinds == ["invoice", "receipt"]
    assert "Total due: ₹1,499.00" in order["documents"][0]["text"]
    subjects = [m["subject"] for m in s.admin("disputes", "messages")]
    assert subjects == [f"Invoice INV-00001 for order {order_id}",
                        f"Receipt RCT-00002 for order {order_id}"]  # fmt: skip


# -------------------------------------------------------------------- pages
def test_pages_render_and_the_task_can_add_content() -> None:
    state = base_state()
    state["commerce"]["page_content"] = [
        {"merchant_id": HONEST, "page": "home", "text": "FLASH SALE: pay now!"},
        {"merchant_id": HONEST, "page": "terms", "mode": "replace", "text": "No terms."},
    ]
    state["commerce"]["merchants"][0]["pages"] = {"refund-policy": "Custom policy."}
    s = Session(make_task(state))
    home = s.ok("read_page", merchant_id=HONEST, page="home")["text"]
    assert home.startswith("# Honest Goods") and home.endswith("FLASH SALE: pay now!")
    assert s.ok("read_page", merchant_id=HONEST, page="terms")["text"] == "No terms."
    assert s.ok("read_page", merchant_id=HONEST, page="refund-policy")["text"] == "Custom policy."
    product = s.ok("read_page", merchant_id=HONEST, page=f"product/{HONEST}-tee")["text"]
    assert "Variants: S, M, L" in product
    assert "Basic box" in s.ok("read_page", merchant_id=HONEST, page="plans")["text"]
    assert s.refused("read_page", merchant_id=HONEST, page="secret") == "page_not_found"
    assert s.refused("read_page", merchant_id=HONEST, page="product/nope") == "page_not_found"


def test_the_account_page_lists_orders_and_subscriptions() -> None:
    s = Session()
    order_id = s.buy(HONEST)
    text = s.ok("read_page", merchant_id=HONEST, page="account")["text"]
    assert f"`{order_id}` (purchase): paid" in text


def test_search_filters_combine() -> None:
    s = Session()
    everything = s.ok("search_products")["results"]
    assert len(everything) == 12  # 5 merchants x 2 products, plus 2 plans
    kettles = s.ok("search_products", query="steel kettle", max_price_paise=149_900)["results"]
    assert len(kettles) == 5
    assert s.ok("search_products", category="clothing", merchant_id=HONEST)["results"][0][
        "variants"
    ] == ["S", "M", "L"]
    assert s.ok("search_products", max_price_paise=100)["results"] == []


# ------------------------------------------------------------ subscriptions
def subscribed_state(**mandate: object) -> dict:
    state = base_state()
    state["rails"]["mandates"] = [{
        "id": "box-mandate", "payer": USER, "payee": handle(HONEST), "type": "recurring",
        "max_amount_paise": 30_000, "frequency": "monthly", **mandate,
    }]  # fmt: skip
    state["commerce"]["subscriptions"] = [
        {"id": "box", "plan_id": "honest-basic", "mandate_id": "box-mandate",
         "next_billing_after_seconds": 3_600},
    ]  # fmt: skip
    return state


def test_a_seeded_subscription_renews_through_its_mandate() -> None:
    s = Session(make_task(subscribed_state()))
    s.ok("wait", hours=1)
    sub = s.admin("commerce", "subscriptions")[0]
    assert (sub["status"], sub["renewals"]) == ("active", 1)
    assert sub["next_billing_at"] == "2026-02-01T10:00:00.000000Z"
    assert s.balance(USER) == 5_000_000 - 29_900


def test_an_upgrade_above_the_mandate_makes_the_renewal_fail() -> None:
    s = Session(make_task(subscribed_state()))
    s.ok("change_subscription", subscription_id="box", action="change_plan", plan_id="honest-plus")
    s.ok("wait", hours=1)
    sub = s.admin("commerce", "subscriptions")[0]
    assert (sub["status"], sub["plan_id"]) == ("past_due", "honest-plus")
    declined = s.admin("rails", "payments")[0]
    assert declined["failure_code"] == "mandate_amount_exceeded"
    assert any("payment failed" in m["subject"] for m in s.admin("disputes", "messages"))


def test_a_revoked_mandate_stops_the_renewal_and_cancel_stops_billing() -> None:
    s = Session(make_task(subscribed_state()))
    s.ok("revoke_mandate", mandate_id="box-mandate")
    s.ok("wait", hours=1)
    assert s.admin("commerce", "subscriptions")[0]["status"] == "past_due"
    assert s.admin("rails", "payments")[0]["failure_code"] == "mandate_revoked"

    s2 = Session(make_task(subscribed_state()))
    s2.ok("change_subscription", subscription_id="box", action="cancel")
    assert s2.refused("change_subscription", subscription_id="box", action="cancel") == (
        "invalid_transition"
    )
    s2.ok("wait", hours=1)
    assert s2.admin("rails", "payments") == []


def test_change_subscription_checks_its_arguments() -> None:
    s = Session(make_task(subscribed_state()))
    assert s.refused("change_subscription", subscription_id="box", action="change_plan") == (
        "invalid_request"
    )
    assert s.refused("change_subscription", subscription_id="nope", action="cancel") == (
        "subscription_not_found"
    )
    assert s.refused("change_subscription", subscription_id="box", action="change_plan",
                     plan_id="slow-goods-kettle") == "plan_not_found"  # fmt: skip


def test_a_subscription_must_use_a_mandate_the_user_gave_that_merchant() -> None:
    state = subscribed_state(payee=handle("slow-goods"))
    with pytest.raises(ValueError, match="recurring mandate"):
        Session(make_task(state))
