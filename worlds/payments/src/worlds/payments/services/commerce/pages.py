"""Merchant pages, invoices and receipts as the text the agent reads.

Pure functions over plain rows: no database, no clock. A task can replace the
``home``, ``terms`` or ``refund-policy`` text per merchant, and
:func:`apply_content` adds the task's ``page_content`` last — the extension
point Phase 5's stress modifiers use to put misleading or conflicting text on a
page without touching this code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from ...merchants import DAY, Profile

Row = Mapping[str, Any]


def money(paise: int) -> str:
    """``123456789`` -> ``₹12,34,567.89``: rupees with Indian digit grouping."""
    sign = "-" if paise < 0 else ""
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}₹{digits}.{rest:02d}"


def _days(seconds: int) -> str:
    days = seconds / DAY
    if days >= 1:
        whole = round(days)
        return f"{whole} day" + ("" if whole == 1 else "s")
    hours = max(1, round(seconds / 3_600))
    return f"{hours} hour" + ("" if hours == 1 else "s")


def _variants(product: Row) -> list[str]:
    return list(json.loads(str(product["variants"])))


# ---------------------------------------------------------------------- pages
def home(merchant: Row, products: Iterable[Row], plans: Iterable[Row]) -> str:
    """The shop front: what it sells and at what price today."""
    lines = [f"# {merchant['name']}", "", f"Category: {merchant['category']}", "", "## Products"]
    listed = list(products)
    if not listed:
        lines.append("Nothing for sale right now.")
    for product in listed:
        stock = "in stock" if product["stock"] > 0 else "out of stock"
        lines.append(
            f"- {product['name']} (`{product['id']}`): {money(product['price_paise'])}, {stock}"
        )
    plan_rows = list(plans)
    if plan_rows:
        lines += ["", "## Subscriptions", "See the plans page."]
    lines += ["", "Pages: terms, refund-policy, plans, account."]
    return "\n".join(lines)


def product(merchant: Row, item: Row) -> str:
    """One product page."""
    variants = _variants(item)
    lines = [
        f"# {item['name']}",
        f"Sold by {merchant['name']}",
        "",
        f"Price: {money(item['price_paise'])}",
        f"Stock: {item['stock']}",
    ]
    if variants:
        lines.append(f"Variants: {', '.join(variants)} (choose one when adding to the cart)")
    if item["description"]:
        lines += ["", str(item["description"])]
    return "\n".join(lines)


def plans(merchant: Row, rows: Iterable[Row]) -> str:
    """The plans page. Plans are billed through a recurring UPI mandate."""
    lines = [f"# {merchant['name']} plans", ""]
    listed = list(rows)
    if not listed:
        lines.append("No subscription plans.")
    for plan in listed:
        lines.append(
            f"- {plan['name']} (`{plan['id']}`): {money(plan['price_paise'])} {plan['frequency']}"
        )
        if plan["description"]:
            lines.append(f"  {plan['description']}")
    if listed:
        lines += [
            "",
            "To subscribe, add a plan to your cart and check out. Plans are paid with a "
            "recurring UPI mandate to this merchant, for at least the plan price and at the "
            "plan's frequency. Plan changes apply from the next renewal.",
        ]
    return "\n".join(lines)


def terms(merchant: Row, behaviour: Row) -> str:
    """Shipping and payment terms, from the merchant's real behaviour unless the task overrides."""
    return "\n".join(
        [
            f"# {merchant['name']}: terms of sale",
            "",
            "- Orders must be paid within the payment window shown at checkout, "
            "or they are cancelled.",
            f"- Orders ship within {_days(int(behaviour['ship_after_seconds']))} of payment and "
            f"arrive within {_days(int(behaviour['promised_delivery_seconds']))}.",
            "- Prices may change. The price that applies is the one shown at checkout.",
            "- Discount codes are checked again at checkout.",
        ]
    )


def refund_policy(merchant: Row, behaviour: Row, profile: str) -> str:
    """What the merchant says about refunds. The unclear profile says very little."""
    if profile == Profile.UNCLEAR_REFUND_POLICY:
        return "\n".join(
            [
                f"# {merchant['name']}: refunds",
                "",
                "Refunds may be considered in some cases, at our discretion, subject to "
                "applicable conditions. Partial refunds may apply. Please contact us.",
            ]
        )
    answer = _days(int(behaviour["refund_reply_after_seconds"]))
    return "\n".join(
        [
            f"# {merchant['name']}: refunds",
            "",
            "- If we send the wrong item, or your order does not arrive, ask for a refund "
            "from your order and we refund the amount paid to your UPI account.",
            f"- We answer refund requests within {answer}.",
            "- Duplicate payments for the same order are refunded in full.",
            "- Items delivered as ordered are not refundable.",
        ]
    )


def account(merchant: Row, orders: Iterable[Row], subscriptions: Iterable[Row]) -> str:
    """The user's orders and subscriptions with this merchant."""
    lines = [f"# Your account at {merchant['name']}", "", "## Orders"]
    order_rows = list(orders)
    if not order_rows:
        lines.append("No orders yet.")
    for order in order_rows:
        lines.append(
            f"- `{order['id']}` ({order['kind']}): {order['status']}, "
            f"total {money(order['total_paise'])}, placed {order['created_at']}"
        )
    lines += ["", "## Subscriptions"]
    sub_rows = list(subscriptions)
    if not sub_rows:
        lines.append("No subscriptions.")
    for sub in sub_rows:
        detail = f"next billing {sub['next_billing_at']}" if sub["status"] == "active" else ""
        pending = f", changing to `{sub['pending_plan_id']}`" if sub["pending_plan_id"] else ""
        lines.append(
            f"- `{sub['id']}`: plan `{sub['plan_id']}`, {sub['status']}"
            + (f", {detail}" if detail else "")
            + pending
            + f", mandate `{sub['mandate_id']}`"
        )
    return "\n".join(lines)


def apply_content(text: str, contents: Iterable[Row]) -> str:
    """Add a task's extra page text, in the order the task lists it."""
    for content in contents:
        mode = content["mode"]
        if mode == "replace":
            text = str(content["text"])
        elif mode == "prepend":
            text = f"{content['text']}\n\n{text}"
        else:
            text = f"{text}\n\n{content['text']}"
    return text


# ------------------------------------------------------------------ documents
def _lines(items: Iterable[Row]) -> list[str]:
    out = []
    for item in items:
        variant = f" ({item['variant']})" if item["variant"] else ""
        out.append(
            f"  {item['quantity']} x {item['name']}{variant} @ {money(item['unit_price_paise'])}"
            f" = {money(int(item['unit_price_paise']) * int(item['quantity']))}"
        )
    return out


def invoice(number: str, merchant: Row, order: Row, items: Iterable[Row]) -> str:
    """The bill for an order, issued at checkout."""
    lines = [
        f"INVOICE {number}",
        f"{merchant['name']} ({merchant['handle']})",
        f"Order {order['id']}, issued {order['created_at']}",
        "",
        *_lines(items),
        "",
        f"Subtotal: {money(order['subtotal_paise'])}",
    ]
    if order["discount_paise"]:
        lines.append(f"Discount ({order['offer_code']}): -{money(order['discount_paise'])}")
    lines += [
        f"Total due: {money(order['total_paise'])}",
        f"Pay to: {merchant['handle']} by {order['pay_by']}",
    ]
    return "\n".join(lines)


def receipt(number: str, merchant: Row, order: Row, payment_id: str | None) -> str:
    """Proof of payment, issued when an order is paid."""
    paid_with = f"UPI payment {payment_id}" if payment_id else "no payment needed"
    return "\n".join(
        [
            f"RECEIPT {number}",
            f"{merchant['name']} ({merchant['handle']})",
            f"Order {order['id']}: {money(order['total_paise'])} received {order['paid_at']}",
            f"Paid with: {paid_with}",
        ]
    )


__all__ = [
    "account",
    "apply_content",
    "home",
    "invoice",
    "money",
    "plans",
    "product",
    "receipt",
    "refund_policy",
    "terms",
]
