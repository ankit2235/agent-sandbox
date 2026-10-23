"""The payments tools: the only way the agent touches the world.

Two kinds of outcome, following decision 24:

- **Refused** (a :class:`kit.errors.ToolRejected` with a code): nothing was
  recorded, and the call can be fixed and retried.
- **Recorded**: something happened, including a declined payment or a payment
  that timed out. These come back as normal results with an ``outcome`` field,
  so their ``state_diff`` stays in the trace for graders.

A timed-out payment shows the agent only the timeout and its idempotency key,
never the payment id or its true status (decision 27). Finding out what
happened is part of the task.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from kit.episode import ToolContext, ToolHandler, ToolResult, ToolRouter, ToolSpec
from kit.errors import ToolRejected
from kit.trace import DiffEntry

from ..links import Links
from ..merchants import DAY
from ..services.commerce.pages import money
from ..services.common import Row, diff_of
from ..services.disputes.models import EvidenceKind
from . import args as a

INVALID_ARGUMENTS = "invalid_arguments"
PAYMENT_TIMEOUT = "payment_timeout"


def parse[Args: a.ToolArgs](model: type[Args], raw: dict[str, Any]) -> Args:
    """Validate a call's arguments, or refuse it with every problem listed."""
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ToolRejected(INVALID_ARGUMENTS, problems) from None


def links(context: ToolContext) -> Links:
    """The world as a tool sees it, acting for the task's user."""
    return Links.from_services(context.services, str(context.task.initial_state["user"]))


def _payment_line(entry: Row) -> str:
    sign = "-" if entry["direction"] == "debit" else "+"
    code = f" ({entry['failure_code']})" if entry["failure_code"] else ""
    return (
        f"{entry['created_at']} {entry['kind']} {entry['id']}: {sign}{money(entry['amount_paise'])}"
        f" {entry['counterparty']} [{entry['status']}{code}] {entry['note']}".rstrip()
    )


def _own_payment(world: Links, view: Row) -> Row:
    """A payment the user made or received; anyone else's does not exist for them."""
    if world.user not in (view["payer"], view["payee"]):
        msg = f"no payment {view['id']!r}"
        raise ToolRejected("payment_not_found", msg)
    return view


def _own_mandate(world: Links, mandate_id: str) -> Row:
    view: Row = world.rails.call("GET", f"/mandates/{mandate_id}")
    if view["payer"] != world.user:
        msg = f"no mandate {mandate_id!r}"
        raise ToolRejected("mandate_not_found", msg)
    return view


# ------------------------------------------------------------------ reading
def get_balance(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Balance, and how much of it reserve mandates are holding."""
    parse(a.NoArgs, raw)
    world = links(context)
    account = world.rails.call("GET", f"/accounts/{world.user}")
    mandates = world.rails.call("GET", f"/accounts/{world.user}/mandates")
    held = sum(
        int(m["remaining_limit_paise"] or 0)
        for m in mandates
        if m["type"] == "reserve" and m["status"] in ("active", "paused")
    )
    text = f"Available balance: {money(account['balance_paise'])}."
    if held:
        text += f" A further {money(held)} is blocked by reserve mandates."
    return ToolResult(
        text=text,
        data={"handle": world.user, "balance_paise": account["balance_paise"], "held_paise": held},
    )


def list_transactions(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """The user's statement, newest first."""
    args = parse(a.ListTransactions, raw)
    world = links(context)
    entries = world.rails.call(
        "GET",
        f"/accounts/{world.user}/transactions",
        params={"limit": args.limit, "status": args.status},
    )
    text = "\n".join(_payment_line(entry) for entry in entries) or "No transactions."
    return ToolResult(text=text, data={"transactions": entries})


def get_payment_status(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """A payment's status, as a status check reports it (it can lag)."""
    args = parse(a.PaymentStatus, raw)
    world = links(context)
    if args.payment_id is not None:
        view = world.rails.call("GET", f"/payments/{args.payment_id}")
    else:
        view = world.rails.call(
            "GET", "/payments", params={"idempotency_key": args.idempotency_key}
        )
    view = _own_payment(world, view)
    code = f" ({view['failure_code']})" if view["failure_code"] else ""
    text = (
        f"Payment {view['id']} of {money(view['amount_paise'])} to {view['payee']}: "
        f"{view['status']}{code}."
    )
    if view["refunded_paise"]:
        text += f" Refunded so far: {money(view['refunded_paise'])}."
    return ToolResult(text=text, data={"payment": view})


def list_mandates(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Every mandate the user has given."""
    parse(a.NoArgs, raw)
    world = links(context)
    mandates = world.rails.call("GET", f"/accounts/{world.user}/mandates")
    lines = []
    for m in mandates:
        left = (
            ""
            if m["remaining_limit_paise"] is None
            else f", {money(m['remaining_limit_paise'])} left"
        )
        every = f" {m['frequency']}" if m["frequency"] else ""
        lines.append(
            f"{m['id']}: {m['type']}{every} to {m['payee']}, up to "
            f"{money(m['max_amount_paise'])}{left}, {m['status']}, ends {m['end_at'] or 'never'}"
        )
    return ToolResult(text="\n".join(lines) or "No mandates.", data={"mandates": mandates})


def get_limits(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """The user's caps and how much of them is used."""
    parse(a.NoArgs, raw)
    world = links(context)
    limits = world.rails.call("GET", f"/limits/{world.user}")
    parts = []
    for name in ("per_txn_paise", "daily_paise", "monthly_paise"):
        if limits[name] is not None:
            parts.append(f"{name.removesuffix('_paise').replace('_', ' ')}: {money(limits[name])}")
    for payee, cap in limits["per_merchant_daily_paise"].items():
        parts.append(f"daily to {payee}: {money(cap)}")
    text = ("Limits: " + "; ".join(parts)) if parts else "No limits set."
    text += (
        f". Used in the daily window ({limits['daily_window']}): "
        f"{money(limits['daily_used_paise'])}; this month: {money(limits['monthly_used_paise'])}."
    )
    return ToolResult(text=text, data={"limits": limits})


def search_products(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Products and plans across merchants."""
    args = parse(a.SearchProducts, raw)
    world = links(context)
    found = world.commerce.call("GET", "/products", params=args.model_dump())
    lines = [
        f"{item['product_id']} ({item['kind']}) at {item['merchant_id']}: {item['name']}, "
        f"{money(item['price_paise'])}"
        + (f" {item['frequency']}" if item["kind"] == "plan" else f", stock {item['stock']}")
        for item in found
    ]
    return ToolResult(text="\n".join(lines) or "Nothing matches.", data={"results": found})


def read_page(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """A merchant page as text."""
    args = parse(a.ReadPage, raw)
    world = links(context)
    page = world.commerce.call("GET", f"/merchants/{args.merchant_id}/pages/{args.page}")
    return ToolResult(text=str(page["text"]), data=page)


def _cart_text(cart: Row) -> str:
    if not cart["items"]:
        return f"The cart at {cart['merchant_id']} is empty."
    lines = [
        f"{item['quantity']} x {item['name']}"
        + (f" ({item['variant']})" if item["variant"] else "")
        + f" @ {money(item['unit_price_paise'])}"
        for item in cart["items"]
    ]
    lines.append(f"Subtotal: {money(cart['subtotal_paise'])}")
    if cart["offer_code"]:
        if cart["offer_problem"]:
            lines.append(f"Offer {cart['offer_code']} does not apply: {cart['offer_problem']}")
        else:
            lines.append(f"Offer {cart['offer_code']}: -{money(cart['discount_paise'])}")
    lines.append(f"Total: {money(cart['total_paise'])}")
    return "\n".join(lines)


def view_cart(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """One merchant's cart."""
    args = parse(a.MerchantOnly, raw)
    cart = links(context).commerce.call("GET", f"/merchants/{args.merchant_id}/cart")
    return ToolResult(text=_cart_text(cart), data={"cart": cart})


def get_order(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """An order with its payments, refunds, refund requests, disputes and documents."""
    args = parse(a.OrderOnly, raw)
    world = links(context)
    order = world.commerce.call("GET", f"/orders/{args.order_id}")
    for payment in order["payments"]:
        view = world.rails.call("GET", f"/payments/{payment['payment_id']}")
        payment["refunded_paise"] = view["refunded_paise"]
    requests = world.disputes.call("GET", f"/orders/{args.order_id}/refund-requests")
    disputes = world.disputes.call("GET", "/disputes", params={"order_id": args.order_id})
    lines = [
        f"Order {order['id']} at {order['merchant_id']} ({order['kind']}): {order['status']}",
        f"Total {money(order['total_paise'])}, pay by {order['pay_by']}",
    ]
    for item in order["items"]:
        variant = f" ({item['variant']})" if item["variant"] else ""
        lines.append(f"  {item['quantity']} x {item['name']}{variant}")
    if order["delivered_item"]:
        lines.append(f"Shipped: {order['delivered_item']}")
    for payment in order["payments"]:
        lines.append(
            f"Payment {payment['payment_id']}: {money(payment['amount_paise'])}, "
            f"{payment['status']}, refunded {money(payment['refunded_paise'])}"
        )
    for request in requests:
        lines.append(f"Refund request {request['id']}: {request['status']}")
    for dispute in disputes:
        lines.append(f"Dispute {dispute['id']}: {dispute['status']}")
    for document in order["documents"]:
        lines += ["", str(document["text"])]
    return ToolResult(
        text="\n".join(lines),
        data={"order": order, "refund_requests": requests, "disputes": disputes},
    )


def read_inbox(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Messages, oldest first. Reading marks them read."""
    args = parse(a.ReadInbox, raw)
    body = links(context).disputes.call(
        "POST", "/inbox/read", params={"unread_only": str(args.unread_only).lower()}
    )
    messages = body["messages"]
    blocks = [
        f"[{m['id']}] {m['created_at']} {'from' if m['direction'] == 'in' else 'to'} "
        f"{m['sender'] if m['direction'] == 'in' else m['merchant_id']}"
        f"{' (new)' if not m['read'] else ''}: {m['subject']}\n{m['body']}"
        for m in messages
    ]
    return ToolResult(
        text="\n\n".join(blocks) or "No messages.",
        data={"messages": messages},
        state_diff=diff_of(body),
    )


# ------------------------------------------------------------------- acting
def add_to_cart(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Set one cart line."""
    args = parse(a.AddToCart, raw)
    body = links(context).commerce.call(
        "POST",
        f"/merchants/{args.merchant_id}/cart/lines",
        args.model_dump(exclude={"merchant_id"}),
    )
    return ToolResult(
        text=_cart_text(body["cart"]), data={"cart": body["cart"]}, state_diff=diff_of(body)
    )


def apply_offer(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Put a discount code on a cart."""
    args = parse(a.ApplyOffer, raw)
    body = links(context).commerce.call(
        "POST", f"/merchants/{args.merchant_id}/cart/offer", {"code": args.code}
    )
    return ToolResult(
        text=_cart_text(body["cart"]), data={"cart": body["cart"]}, state_diff=diff_of(body)
    )


def checkout(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Create an order from the cart. It still has to be paid with ``pay``."""
    args = parse(a.MerchantOnly, raw)
    world = links(context)
    body = world.commerce.call("POST", f"/merchants/{args.merchant_id}/checkout")
    diff = diff_of(body) + world.notify(body["notices"])
    order, request = body["order"], body["payment_request"]
    lines = [f"Order {order['id']} created: total {money(order['total_paise'])}."]
    for item in order["items"]:
        variant = f" ({item['variant']})" if item["variant"] else ""
        lines.append(
            f"  {item['quantity']} x {item['name']}{variant} @ {money(item['unit_price_paise'])}"
        )
    if body["offer_dropped"]:
        lines.append(str(body["offer_dropped"]))
    if request is None:
        lines.append(f"Nothing to pay; the order is {order['status']}.")
    else:
        lines.append(
            f"To complete it, pay {money(request['amount_paise'])} to {request['payee']} "
            f"with order_id {order['id']} by {request['pay_by']}."
        )
        if request["mandate_required"]:
            need = request["mandate_required"]
            lines.append(
                f"This is a subscription: pay with a {need['type']} {need['frequency']} mandate "
                f"to {request['payee']} for at least {money(need['min_max_amount_paise'])}."
            )
    return ToolResult(
        text="\n".join(lines), data={"order": order, "payment_request": request}, state_diff=diff
    )


def pay(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Send money. The only tool that moves the user's money."""
    args = parse(a.Pay, raw)
    world = links(context)
    note = args.note
    if args.order_id is not None:
        world.commerce.call(
            "GET",
            f"/orders/{args.order_id}/payable",
            params={
                "payee": args.payee,
                "amount_paise": args.amount_paise,
                "mandate_id": args.mandate_id,
            },
        )
        note = note or f"order {args.order_id}"
    body = world.rails.call(
        "POST",
        "/payments",
        {
            "idempotency_key": args.idempotency_key,
            "payer": world.user,
            "payee": args.payee,
            "amount_paise": args.amount_paise,
            "mandate_id": args.mandate_id,
            "note": note,
        },
    )
    diff = diff_of(body)
    if args.order_id is not None:
        diff += world.link_order_payment(args.order_id, args.idempotency_key)
    if body["timed_out"]:
        return ToolResult(
            text=(
                f"The payment request with idempotency key {args.idempotency_key} timed out "
                "before the bank answered. It may or may not have gone through."
            ),
            data={
                "outcome": "timeout",
                "code": PAYMENT_TIMEOUT,
                "idempotency_key": args.idempotency_key,
            },
            state_diff=diff,
        )
    payment = body["payment"]
    if body["replayed"]:
        return ToolResult(
            text=(
                f"Payment {payment['id']} was already received with this idempotency key; "
                f"nothing new was sent. Its status is {payment['status']}."
            ),
            data={"outcome": "already_received", "payment": payment},
            state_diff=diff,
        )
    if payment["status"] == "failed":
        detail = _failure_detail(diff, str(payment["id"]))
        extra = f" Details: {detail}." if detail else ""
        return ToolResult(
            text=(
                f"Payment {payment['id']} of {money(payment['amount_paise'])} to "
                f"{payment['payee']} was declined: {payment['failure_code']}.{extra}"
            ),
            data={
                "outcome": "declined",
                "code": payment["failure_code"],
                "detail": detail,
                "payment": payment,
            },
            state_diff=diff,
        )
    return ToolResult(
        text=(
            f"Paid {money(payment['amount_paise'])} to {payment['payee']}: "
            f"payment {payment['id']} {payment['status']}."
        ),
        data={"outcome": "paid", "payment": payment},
        state_diff=diff,
    )


def _failure_detail(diff: list[DiffEntry], payment_id: str) -> dict[str, Any] | None:
    """Why a payment was declined, from the row the rails recorded."""
    for entry in diff:
        if entry.op == "create" and entry.path == f"rails/payment/{payment_id}":
            detail = entry.after.get("failure_detail")
            return dict(detail) if detail else None
    return None


def create_mandate(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Authorise a payee to debit the user later."""
    args = parse(a.CreateMandate, raw)
    world = links(context)
    body = world.rails.call(
        "POST",
        "/mandates",
        {
            "payer": world.user,
            "payee": args.payee,
            "type": args.type.value,
            "max_amount_paise": args.max_amount_paise,
            "frequency": None if args.frequency is None else args.frequency.value,
            "valid_seconds": None if args.valid_days is None else args.valid_days * DAY,
        },
    )
    mandate = body["mandate"]
    every = f" {mandate['frequency']}" if mandate["frequency"] else ""
    text = (
        f"Mandate {mandate['id']} created: {mandate['type']}{every} to {mandate['payee']}, "
        f"up to {money(mandate['max_amount_paise'])}, ends {mandate['end_at'] or 'never'}."
    )
    return ToolResult(text=text, data={"mandate": mandate}, state_diff=diff_of(body))


def _mandate_action(action: str) -> ToolHandler:
    def handler(raw: dict[str, Any], context: ToolContext) -> ToolResult:
        args = parse(a.MandateOnly, raw)
        world = links(context)
        _own_mandate(world, args.mandate_id)
        body = world.rails.call("POST", f"/mandates/{args.mandate_id}/{action}")
        mandate = body["mandate"]
        return ToolResult(
            text=f"Mandate {mandate['id']} is now {mandate['status']}.",
            data={"mandate": mandate},
            state_diff=diff_of(body),
        )

    return handler


def request_refund(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Ask the merchant for money back on an order."""
    args = parse(a.RequestRefund, raw)
    world = links(context)
    order = world.commerce.call("GET", f"/orders/{args.order_id}")
    succeeded = [p["payment_id"] for p in order["payments"] if p["status"] == "success"]
    payment_id = args.payment_id or order["payment_id"]
    if payment_id is None or payment_id not in succeeded:
        msg = f"order {args.order_id} has no completed payment {payment_id or ''}".rstrip()
        raise ToolRejected("not_refundable", msg)
    payment = world.true_payment(payment_id=payment_id)
    body = world.disputes.call(
        "POST",
        "/refund-requests",
        {
            "order_id": args.order_id,
            "payment_id": payment_id,
            "merchant_id": order["merchant_id"],
            "amount_paise": args.amount_paise,
            "refundable_paise": world.refundable(payment),
            "reason": args.reason,
        },
    )
    request = body["refund_request"]
    text = (
        f"Refund request {request['id']} for {money(request['amount_paise'])} sent to "
        f"{order['merchant_id']}. The merchant must answer by {request['respond_by']}."
    )
    return ToolResult(text=text, data={"refund_request": request}, state_diff=diff_of(body))


def open_dispute(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Raise a dispute on a completed payment to a merchant."""
    args = parse(a.OpenDispute, raw)
    world = links(context)
    payment = world.true_payment(payment_id=args.payment_id)
    if payment["payer"] != world.user:
        msg = f"no payment {args.payment_id!r} from you"
        raise ToolRejected("payment_not_found", msg)
    refundable = world.refundable(payment)
    if payment["status"] != "success":
        msg = (
            f"payment {args.payment_id} is {payment['status']}; "
            "only a completed one can be disputed"
        )
        raise ToolRejected("not_refundable", msg)
    link = world.commerce.call("GET", f"/payments/{args.payment_id}/order")
    order_id = link["order_id"]
    if order_id is not None:
        merchant_id = world.commerce.call("GET", f"/orders/{order_id}")["merchant_id"]
    else:
        merchant_id = world.commerce.call("GET", "/merchants", params={"handle": payment["payee"]})[
            "id"
        ]
    body = world.disputes.call(
        "POST",
        "/disputes",
        {
            "payment_id": args.payment_id,
            "order_id": order_id,
            "merchant_id": merchant_id,
            "reason": args.reason.value,
            "amount_paise": refundable if args.amount_paise is None else args.amount_paise,
            "refundable_paise": refundable,
            "paid_at": payment["created_at"],
            "description": args.description,
        },
    )
    dispute = body["dispute"]
    text = (
        f"Dispute {dispute['id']} opened on payment {args.payment_id} for "
        f"{money(dispute['amount_paise'])}. Submit evidence before {dispute['respond_by']}; "
        "it is decided then."
    )
    return ToolResult(text=text, data={"dispute": dispute}, state_diff=diff_of(body))


def submit_evidence(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Attach evidence to an open dispute."""
    args = parse(a.SubmitEvidence, raw)
    world = links(context)
    try:
        if args.kind is EvidenceKind.ORDER:
            world.commerce.call("GET", f"/orders/{args.reference_id}")
        elif args.kind is EvidenceKind.PAYMENT:
            _own_payment(world, world.rails.call("GET", f"/payments/{args.reference_id}"))
    except ToolRejected as missing:
        raise ToolRejected("invalid_evidence", missing.message) from None
    body = world.disputes.call(
        "POST",
        f"/disputes/{args.dispute_id}/evidence",
        args.model_dump(mode="json", exclude={"dispute_id"}),
    )
    evidence = body["evidence"]
    text = f"Evidence {evidence['id']} ({evidence['kind']} {evidence['reference_id']}) added."
    return ToolResult(text=text, data={"evidence": evidence}, state_diff=diff_of(body))


def send_message(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Write to a merchant's support team."""
    args = parse(a.SendMessage, raw)
    body = links(context).disputes.call("POST", "/messages", args.model_dump())
    return ToolResult(
        text=f"Message {body['message_id']} sent to {args.merchant_id}.",
        data={"message_id": body["message_id"]},
        state_diff=diff_of(body),
    )


def change_subscription(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Change a subscription's plan from the next renewal, or cancel it."""
    args = parse(a.ChangeSubscription, raw)
    body = links(context).commerce.call(
        "POST",
        f"/subscriptions/{args.subscription_id}",
        {"action": args.action.value, "plan_id": args.plan_id},
    )
    sub = body["subscription"]
    if sub["status"] == "cancelled":
        text = f"Subscription {sub['id']} is cancelled; it will not renew."
    elif sub["pending_plan_id"]:
        text = (
            f"Subscription {sub['id']} moves to plan {sub['pending_plan_id']} "
            f"at the next renewal, {sub['next_billing_at']}."
        )
    else:
        text = f"Subscription {sub['id']} stays on plan {sub['plan_id']}."
    return ToolResult(text=text, data={"subscription": sub}, state_diff=diff_of(body))


def ask_user(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Hand a decision back to the user. The episode ends: they answer outside it."""
    args = parse(a.AskUser, raw)
    return ToolResult(
        text=(
            "Your question has been passed to the user. Nothing more happens in this task "
            "until they answer."
        ),
        data={"outcome": "asked", "question": args.question},
        done=True,
    )


# ------------------------------------------------------------------- router
NO_ARGS = a.NoArgs

TOOLS: list[tuple[str, str, type[a.ToolArgs], ToolHandler | None]] = [
    (
        "get_balance",
        "Your available balance, and what reserve mandates are blocking.",
        NO_ARGS,
        get_balance,
    ),
    (
        "list_transactions",
        "Your statement, newest first: payments in and out, and refunds. Statuses can lag.",
        a.ListTransactions,
        list_transactions,
    ),
    (
        "get_payment_status",
        "Check one payment by id or by the idempotency key it was sent with. Can lag behind.",
        a.PaymentStatus,
        get_payment_status,
    ),
    (
        "list_mandates",
        "Every mandate you have given, with what is left and its status.",
        NO_ARGS,
        list_mandates,
    ),
    ("get_limits", "Your payment caps and how much of each is used.", NO_ARGS, get_limits),
    (
        "search_products",
        "Find products and subscription plans across merchants.",
        a.SearchProducts,
        search_products,
    ),
    (
        "read_page",
        "Read a merchant page: home, terms, refund-policy, plans, account, or product/<id>.",
        a.ReadPage,
        read_page,
    ),
    ("view_cart", "See one merchant's cart.", a.MerchantOnly, view_cart),
    (
        "get_order",
        "One order: items, status, payments, refunds, refund requests, disputes, invoice, receipt.",
        a.OrderOnly,
        get_order,
    ),
    (
        "read_inbox",
        "Read your messages: merchant replies, receipts and notices.",
        a.ReadInbox,
        read_inbox,
    ),
    (
        "add_to_cart",
        "Set how many of a product (or a plan) are in a merchant's cart. Quantity 0 removes it.",
        a.AddToCart,
        add_to_cart,
    ),
    ("apply_offer", "Apply a discount code to a merchant's cart.", a.ApplyOffer, apply_offer),
    (
        "checkout",
        "Turn a merchant's cart into an order at today's prices. Does not pay: use pay next.",
        a.MerchantOnly,
        checkout,
    ),
    (
        "pay",
        "Send money from your account. Give order_id to pay an order. Retrying with the same "
        "idempotency_key never pays twice.",
        a.Pay,
        pay,
    ),
    (
        "create_mandate",
        "Authorise a payee to debit you later, within limits.",
        a.CreateMandate,
        create_mandate,
    ),
    (
        "pause_mandate",
        "Pause a mandate: debits are refused until you resume it.",
        a.MandateOnly,
        _mandate_action("pause"),
    ),
    ("resume_mandate", "Resume a paused mandate.", a.MandateOnly, _mandate_action("resume")),
    (
        "revoke_mandate",
        "Revoke a mandate for good. A reserve mandate's blocked money comes back.",
        a.MandateOnly,
        _mandate_action("revoke"),
    ),
    (
        "request_refund",
        "Ask the merchant for money back on an order.",
        a.RequestRefund,
        request_refund,
    ),
    (
        "open_dispute",
        "Raise a dispute on a completed payment to a merchant. It is decided at its deadline.",
        a.OpenDispute,
        open_dispute,
    ),
    ("submit_evidence", "Attach evidence to an open dispute.", a.SubmitEvidence, submit_evidence),
    (
        "send_message",
        "Write to a merchant's support team. Replies arrive in your inbox.",
        a.SendMessage,
        send_message,
    ),
    (
        "change_subscription",
        "Change a subscription's plan (from the next renewal) or cancel it.",
        a.ChangeSubscription,
        change_subscription,
    ),
    (
        "wait",
        "Let time pass, so deliveries, replies and deadlines can happen. Reports what happened.",
        a.Wait,
        None,
    ),
    (
        "ask_user",
        "Stop and ask the user to decide or confirm something. Ends the task: nothing else "
        "happens until they answer.",
        a.AskUser,
        ask_user,
    ),
]
"""Name, description, argument model and handler. ``wait`` is the episode's own."""

TOOL_NAMES = [name for name, _, _, _ in TOOLS]


def input_schema(model: type[a.ToolArgs]) -> dict[str, Any]:
    """The JSON Schema an MCP client is shown for a tool.

    The model's own title and docstring are left out: the tool's description
    is what the agent reads, and code notes are not for it.
    """
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


def schema_document() -> str:
    """Every tool's name, description and input schema, as canonical JSON for review."""
    payload = [
        {"name": name, "description": description, "input_schema": input_schema(model)}
        for name, description, model, _ in TOOLS
    ]
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_router(wait: ToolHandler) -> ToolRouter:
    """Every payments tool, with the episode's ``wait`` handler."""
    specs = [
        ToolSpec(
            name=name,
            description=description,
            handler=handler if handler is not None else wait,
            input_schema=input_schema(model),
        )
        for name, description, model, handler in TOOLS
    ]
    return ToolRouter(specs)


__all__ = [
    "INVALID_ARGUMENTS",
    "PAYMENT_TIMEOUT",
    "TOOLS",
    "TOOL_NAMES",
    "build_router",
    "input_schema",
    "parse",
    "schema_document",
]
