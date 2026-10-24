"""Commerce rules over one SQLite database per episode.

Merchants, catalogue, offers, carts, orders and subscriptions. Commerce never
moves money and never calls the rails: an order asks to be paid (checkout
returns a payment request), the episode pays it through the rails and tells
commerce what happened (:meth:`Commerce.link_payment`). Subscription renewals
work the same way in reverse: a renewal event returns a ``charge`` the episode
collects through the user's mandate.

Anything the user should hear about (an invoice, a receipt, a shipment) comes
back as a ``notice``; the episode delivers notices to the support inbox.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.service import SqliteStore
from kit.trace import DiffEntry

from ...merchants import Behaviour
from ..common import Result, Row, SqlCore, iso, parse
from ..rails.models import IST, Frequency
from . import pages
from .models import (
    PAGES,
    CartLine,
    CommerceConfig,
    LoadRequest,
    OrderKind,
    OrderStatus,
    PaymentLink,
    PaymentStatusUpdate,
    Rejection,
    SubscriptionChange,
    SubscriptionStatus,
)

EVENT_ORDER_EXPIRY = "commerce.order_expiry"
EVENT_SHIP = "commerce.ship"
EVENT_DELIVER = "commerce.deliver"
EVENT_RENEWAL = "commerce.subscription_renewal"

ADMIN_RESOURCES = (
    "state",
    "merchants",
    "products",
    "plans",
    "offers",
    "carts",
    "orders",
    "order_items",
    "order_payments",
    "documents",
    "subscriptions",
)

SCHEMA = """
CREATE TABLE commerce_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE merchant (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    handle TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    profile TEXT NOT NULL,
    behaviour TEXT NOT NULL,
    pages TEXT NOT NULL,
    paid_orders INTEGER NOT NULL
) STRICT;

CREATE TABLE product (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0),
    variants TEXT NOT NULL,
    description TEXT NOT NULL,
    price_changed INTEGER NOT NULL CHECK (price_changed IN (0, 1))
) STRICT;

CREATE TABLE plan (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    name TEXT NOT NULL,
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    description TEXT NOT NULL
) STRICT;

CREATE TABLE offer (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    code TEXT NOT NULL,
    percent_off INTEGER,
    flat_off_paise INTEGER,
    max_discount_paise INTEGER,
    min_cart_paise INTEGER NOT NULL,
    product_ids TEXT NOT NULL,
    first_order_only INTEGER NOT NULL,
    expires_at TEXT,
    UNIQUE (merchant_id, code)
) STRICT;

CREATE TABLE cart (
    merchant_id TEXT PRIMARY KEY REFERENCES merchant (id),
    offer_code TEXT
) STRICT;

CREATE TABLE cart_line (
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    item_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_paise INTEGER NOT NULL,
    PRIMARY KEY (merchant_id, item_id, variant)
) STRICT;

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    kind TEXT NOT NULL CHECK (kind IN ('purchase', 'subscription', 'renewal')),
    status TEXT NOT NULL CHECK (
        status IN ('created', 'paid', 'shipped', 'delivered', 'wrong_item', 'cancelled')
    ),
    subtotal_paise INTEGER NOT NULL,
    discount_paise INTEGER NOT NULL,
    total_paise INTEGER NOT NULL CHECK (total_paise >= 0),
    offer_code TEXT,
    plan_id TEXT,
    subscription_id TEXT,
    created_at TEXT NOT NULL,
    pay_by TEXT NOT NULL,
    paid_at TEXT,
    payment_id TEXT,
    ships_wrong_item INTEGER NOT NULL CHECK (ships_wrong_item IN (0, 1)),
    expected_delivery_at TEXT,
    shipped_at TEXT,
    delivered_at TEXT,
    delivered_item TEXT,
    cancel_reason TEXT
) STRICT;

CREATE TABLE order_item (
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders (id),
    item_id TEXT NOT NULL,
    name TEXT NOT NULL,
    variant TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    cart_unit_price_paise INTEGER NOT NULL,
    unit_price_paise INTEGER NOT NULL
) STRICT;

CREATE TABLE order_payment (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders (id),
    amount_paise INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed', 'reversed')),
    mandate_id TEXT,
    linked_at TEXT NOT NULL
) STRICT;

CREATE TABLE document (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders (id),
    kind TEXT NOT NULL CHECK (kind IN ('invoice', 'receipt')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE subscription (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    plan_id TEXT NOT NULL REFERENCES plan (id),
    mandate_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'past_due', 'cancelled')),
    started_at TEXT NOT NULL,
    next_billing_at TEXT,
    pending_plan_id TEXT,
    renewals INTEGER NOT NULL
) STRICT;

CREATE TABLE page_content (
    id INTEGER PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchant (id),
    page TEXT NOT NULL,
    mode TEXT NOT NULL,
    text TEXT NOT NULL
) STRICT;
"""


def raised(price: int, bps: int) -> int:
    """A price after a rise of ``bps`` basis points, rounded half up to the paisa."""
    return (price * (10_000 + bps) + 5_000) // 10_000


def next_period(moment: dt.datetime, frequency: Frequency) -> dt.datetime:
    """The same moment one billing period later, counted in IST like the rails' mandates.

    A monthly date that does not exist (31 January -> February) falls back to the
    month's last day.
    """
    if frequency is Frequency.DAILY:
        return moment + dt.timedelta(days=1)
    if frequency is Frequency.WEEKLY:
        return moment + dt.timedelta(days=7)
    local = moment.astimezone(IST)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    first_of_following = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last_day = (first_of_following - dt.timedelta(days=1)).day
    return local.replace(year=year, month=month, day=min(local.day, last_day)).astimezone(dt.UTC)


def notice(merchant: Row, subject: str, body: str, order_id: str | None = None) -> Row:
    """Something the user should be told, for the episode to deliver to the inbox."""
    return {"merchant_id": merchant["id"], "subject": subject, "body": body, "order_id": order_id}


class Commerce(SqlCore):
    """Merchants, catalogue, offers, carts, orders and subscriptions."""

    service = "commerce"

    def __init__(self, rng: SeededRNG, store: SqliteStore | None = None) -> None:
        super().__init__(rng, SCHEMA, store)
        self._config_cache: tuple[str, CommerceConfig] | None = None

    # ------------------------------------------------------------- plumbing
    def _config(self) -> CommerceConfig:
        row = self._one("SELECT value FROM commerce_config WHERE key = 'config'")
        if row is None:
            msg = "commerce has not been loaded with a task yet"
            raise ToolRejected(Rejection.INVALID_REQUEST, msg)
        raw = str(row["value"])
        if self._config_cache is None or self._config_cache[0] != raw:
            self._config_cache = (raw, CommerceConfig.model_validate_json(raw))
        return self._config_cache[1]

    def _user(self) -> str:
        row = self._one("SELECT value FROM commerce_config WHERE key = 'user'")
        assert row is not None  # written together with the config
        return str(row["value"])

    def _merchant(self, merchant_id: str) -> Row:
        row = self._one("SELECT * FROM merchant WHERE id = ?", (merchant_id,))
        if row is None:
            msg = f"no merchant {merchant_id!r}"
            raise ToolRejected(Rejection.MERCHANT_NOT_FOUND, msg)
        return row

    @staticmethod
    def _behaviour(merchant: Row) -> Behaviour:
        return Behaviour.model_validate_json(str(merchant["behaviour"]))

    def _order(self, order_id: str) -> Row:
        row = self._one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            msg = f"no order {order_id!r}"
            raise ToolRejected(Rejection.ORDER_NOT_FOUND, msg)
        return row

    # ----------------------------------------------------------------- load
    def load(self, request: LoadRequest, now: dt.datetime) -> Result:
        """Set up a task's merchants, catalogue and subscriptions. Loaded once."""
        config = request.config
        stamp = iso(now)
        scheduled: list[ScheduledEvent] = []
        with self._transaction():
            if self._scalar("SELECT COUNT(*) FROM commerce_config"):
                msg = "commerce is already loaded; build a new service for a new task"
                raise ToolRejected(Rejection.ALREADY_LOADED, msg)
            self._db.executemany(
                "INSERT INTO commerce_config VALUES (?, ?)",
                [("config", config.model_dump_json()), ("user", request.user)],
            )
            for merchant in config.merchants:
                self._insert(
                    "merchant",
                    {
                        "id": merchant.id,
                        "name": merchant.name,
                        "handle": merchant.handle,
                        "category": merchant.category,
                        "profile": merchant.profile.value,
                        "behaviour": merchant.resolved().model_dump_json(),
                        "pages": json.dumps(merchant.pages, sort_keys=True),
                        "paid_orders": 0,
                    },
                )
            for product in config.products:
                self._insert(
                    "product",
                    {
                        **product.model_dump(exclude={"variants"}),
                        "variants": json.dumps(product.variants),
                        "price_changed": 0,
                    },
                )
            for plan in config.plans:
                self._insert("plan", plan.model_dump(mode="json"))
            for offer in config.offers:
                expires = (
                    None
                    if offer.expires_after_seconds is None
                    else iso(now + dt.timedelta(seconds=offer.expires_after_seconds))
                )
                self._insert(
                    "offer",
                    {
                        "id": f"{offer.merchant_id}:{offer.code}",
                        **offer.model_dump(exclude={"product_ids", "expires_after_seconds"}),
                        "product_ids": json.dumps(offer.product_ids),
                        "expires_at": expires,
                    },
                )
            plans = {plan.id: plan for plan in config.plans}
            for sub in config.subscriptions:
                plan = plans[sub.plan_id]
                at = now + dt.timedelta(seconds=sub.next_billing_after_seconds)
                self._insert(
                    "subscription",
                    {
                        "id": sub.id,
                        "merchant_id": plan.merchant_id,
                        "plan_id": plan.id,
                        "mandate_id": sub.mandate_id,
                        "status": SubscriptionStatus.ACTIVE.value,
                        "started_at": stamp,
                        "next_billing_at": iso(at),
                        "pending_plan_id": None,
                        "renewals": 0,
                    },
                )
                scheduled.append(
                    ScheduledEvent(at=at, name=EVENT_RENEWAL, payload={"subscription_id": sub.id})
                )
            for content in config.page_content:
                self._db.execute(
                    "INSERT INTO page_content (merchant_id, page, mode, text) VALUES (?, ?, ?, ?)",
                    (content.merchant_id, content.page, content.mode, content.text),
                )
        counts = {"merchants": len(config.merchants), "products": len(config.products)}
        return Result(body={"loaded": counts}, scheduled=scheduled)

    # ----------------------------------------------------------------- carts
    def _item(self, item_id: str, merchant_id: str) -> tuple[str, Row]:
        product = self._one(
            "SELECT * FROM product WHERE id = ? AND merchant_id = ?", (item_id, merchant_id)
        )
        if product is not None:
            return "product", product
        plan = self._one(
            "SELECT * FROM plan WHERE id = ? AND merchant_id = ?", (item_id, merchant_id)
        )
        if plan is not None:
            return "plan", plan
        msg = f"{merchant_id!r} sells no product or plan {item_id!r}"
        raise ToolRejected(Rejection.PRODUCT_NOT_FOUND, msg)

    def set_cart_line(self, merchant_id: str, line: CartLine, now: dt.datetime) -> Result:
        """Set how many of one item (and variant) the cart holds. Quantity 0 removes it."""
        with self._transaction():
            self._config()
            self._merchant(merchant_id)
            kind, item = self._item(line.product_id, merchant_id)
            variant = line.variant or ""
            if kind == "product":
                variants = json.loads(str(item["variants"]))
                if variants and not variant:
                    msg = f"{item['name']} comes in {variants}; choose a variant"
                    raise ToolRejected(Rejection.VARIANT_REQUIRED, msg)
                if variant and variant not in variants:
                    msg = f"{item['name']} has no variant {variant!r}; it has {variants}"
                    raise ToolRejected(Rejection.VARIANT_NOT_FOUND, msg)
                if line.quantity > int(item["stock"]):
                    msg = f"only {item['stock']} of {item['name']} in stock"
                    raise ToolRejected(Rejection.OUT_OF_STOCK, msg)
            elif variant:
                msg = "plans have no variants"
                raise ToolRejected(Rejection.VARIANT_NOT_FOUND, msg)
            lines = self._cart_lines(merchant_id)
            others = [
                row for row in lines if (row["item_id"], row["variant"]) != (item["id"], variant)
            ]
            if line.quantity and others:
                other_kinds = {self._item(str(row["item_id"]), merchant_id)[0] for row in others}
                if kind == "plan" or "plan" in other_kinds:
                    msg = "a plan is checked out on its own; empty the cart first"
                    raise ToolRejected(Rejection.PLAN_IN_CART, msg)
            if kind == "plan" and line.quantity > 1:
                msg = "a plan's quantity is 1"
                raise ToolRejected(Rejection.INVALID_REQUEST, msg)
            key = (merchant_id, item["id"], variant)
            before = self._one(
                "SELECT * FROM cart_line WHERE merchant_id = ? AND item_id = ? AND variant = ?", key
            )
            path = f"commerce/cart/{merchant_id}/{item['id']}" + (f":{variant}" if variant else "")
            diff: list[DiffEntry] = []
            if line.quantity == 0:
                if before is not None:
                    self._db.execute(
                        "DELETE FROM cart_line"
                        " WHERE merchant_id = ? AND item_id = ? AND variant = ?",
                        key,
                    )
                    diff.append(DiffEntry(op="delete", path=path, before=before))
            else:
                after = {
                    "merchant_id": merchant_id,
                    "item_id": item["id"],
                    "variant": variant,
                    "quantity": line.quantity,
                    "unit_price_paise": item["price_paise"],
                }
                self._db.execute(
                    "INSERT OR REPLACE INTO cart_line VALUES (?, ?, ?, ?, ?)", tuple(after.values())
                )
                self._db.execute(
                    "INSERT OR IGNORE INTO cart (merchant_id) VALUES (?)", (merchant_id,)
                )
                diff.append(
                    DiffEntry(
                        op="create" if before is None else "update",
                        path=path,
                        before=before,
                        after=after,
                    )
                )
            view = self._cart_view(merchant_id, now)
        return Result(body={"cart": view}, state_diff=diff)

    def _cart_lines(self, merchant_id: str) -> list[Row]:
        return self.store.query_all(
            "SELECT * FROM cart_line WHERE merchant_id = ? ORDER BY item_id, variant",
            (merchant_id,),
        )

    def _cart_view(self, merchant_id: str, now: dt.datetime) -> Row:
        lines = self._cart_lines(merchant_id)
        items = []
        for line in lines:
            _, item = self._item(str(line["item_id"]), merchant_id)
            items.append(
                {
                    "product_id": line["item_id"],
                    "name": item["name"],
                    "variant": line["variant"] or None,
                    "quantity": line["quantity"],
                    "unit_price_paise": line["unit_price_paise"],
                    "line_total_paise": int(line["unit_price_paise"]) * int(line["quantity"]),
                }
            )
        subtotal = sum(int(item["line_total_paise"]) for item in items)
        cart = self._one("SELECT offer_code FROM cart WHERE merchant_id = ?", (merchant_id,))
        code = None if cart is None else cart["offer_code"]
        discount, problem = 0, None
        if code is not None:
            discount, problem = self._discount(merchant_id, str(code), lines, now)
        return {
            "merchant_id": merchant_id,
            "items": items,
            "subtotal_paise": subtotal,
            "offer_code": code,
            "offer_problem": problem,
            "discount_paise": 0 if problem else discount,
            "total_paise": subtotal - (0 if problem else discount),
        }

    def cart(self, merchant_id: str, now: dt.datetime) -> Row:
        """What is in one merchant's cart, at the prices shown when each line was added."""
        self._config()
        self._merchant(merchant_id)
        return self._cart_view(merchant_id, now)

    # ---------------------------------------------------------------- offers
    def _discount(
        self, merchant_id: str, code: str, lines: list[Row], now: dt.datetime
    ) -> tuple[int, str | None]:
        """The discount a code gives on these lines, or why it gives none."""
        offer = self._one(
            "SELECT * FROM offer WHERE merchant_id = ? AND code = ?", (merchant_id, code.upper())
        )
        if offer is None:
            return 0, Rejection.OFFER_NOT_FOUND.value
        if offer["expires_at"] is not None and now >= parse(str(offer["expires_at"])):
            return 0, Rejection.OFFER_EXPIRED.value
        plans = {row["id"] for row in self.store.query_all("SELECT id FROM plan")}
        if any(line["item_id"] in plans for line in lines):
            return 0, f"{Rejection.OFFER_CONDITIONS_NOT_MET.value}: offers do not apply to plans"
        subtotal = sum(int(line["unit_price_paise"]) * int(line["quantity"]) for line in lines)
        if not lines or subtotal < int(offer["min_cart_paise"]):
            return (
                0,
                f"{Rejection.OFFER_CONDITIONS_NOT_MET.value}: "
                f"minimum cart {pages.money(int(offer['min_cart_paise']))}",
            )
        product_ids = json.loads(str(offer["product_ids"]))
        eligible = subtotal
        if product_ids:
            eligible = sum(
                int(line["unit_price_paise"]) * int(line["quantity"])
                for line in lines
                if line["item_id"] in product_ids
            )
            if eligible == 0:
                return 0, f"{Rejection.OFFER_CONDITIONS_NOT_MET.value}: only for {product_ids}"
        if offer["first_order_only"] and self._has_paid_order(merchant_id):
            return 0, f"{Rejection.OFFER_CONDITIONS_NOT_MET.value}: first order only"
        if offer["percent_off"] is not None:
            discount = eligible * int(offer["percent_off"]) // 100
        else:
            discount = int(offer["flat_off_paise"])
        if offer["max_discount_paise"] is not None:
            discount = min(discount, int(offer["max_discount_paise"]))
        return min(discount, eligible), None

    def _open_payments(self, order_id: str) -> int:
        """Payments for an order that went through or may still go through."""
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM order_payment"
                " WHERE order_id = ? AND status IN ('pending', 'success')",
                (order_id,),
            )
        )

    def _has_paid_order(self, merchant_id: str) -> bool:
        return bool(
            self._scalar(
                "SELECT COUNT(*) FROM orders WHERE merchant_id = ? AND paid_at IS NOT NULL",
                (merchant_id,),
            )
        )

    def apply_offer(self, merchant_id: str, code: str, now: dt.datetime) -> Result:
        """Put a discount code on a cart, if it applies to the cart as it is now."""
        with self._transaction():
            self._config()
            self._merchant(merchant_id)
            discount, problem = self._discount(
                merchant_id, code, self._cart_lines(merchant_id), now
            )
            if problem is not None:
                reason, _, detail = problem.partition(": ")
                messages = {
                    Rejection.OFFER_NOT_FOUND.value: f"{merchant_id!r} has no offer {code!r}",
                    Rejection.OFFER_EXPIRED.value: f"offer {code!r} has expired",
                }
                msg = messages.get(reason, f"offer {code!r} does not apply: {detail}")
                raise ToolRejected(reason, msg)
            before = self._one("SELECT * FROM cart WHERE merchant_id = ?", (merchant_id,))
            self._db.execute(
                "INSERT INTO cart (merchant_id, offer_code) VALUES (?, ?)"
                " ON CONFLICT (merchant_id) DO UPDATE SET offer_code = excluded.offer_code",
                (merchant_id, code.upper()),
            )
            diff = [
                DiffEntry(
                    op="update",
                    path=f"commerce/cart/{merchant_id}/offer_code",
                    before=None if before is None else before["offer_code"],
                    after=code.upper(),
                )
            ]
            view = self._cart_view(merchant_id, now)
        return Result(body={"cart": view, "discount_paise": discount}, state_diff=diff)

    # -------------------------------------------------------------- checkout
    def checkout(self, merchant_id: str, now: dt.datetime) -> Result:
        """Turn a cart into an order at today's prices, and ask to be paid for it."""
        with self._transaction():
            config = self._config()
            merchant = self._merchant(merchant_id)
            behaviour = self._behaviour(merchant)
            lines = self._cart_lines(merchant_id)
            if not lines:
                msg = f"the cart at {merchant_id!r} is empty"
                raise ToolRejected(Rejection.CART_EMPTY, msg)
            diff: list[DiffEntry] = []
            items: list[Row] = []
            plan: Row | None = None
            for line in lines:
                kind, item = self._item(str(line["item_id"]), merchant_id)
                if kind == "plan":
                    plan = item
                    price = int(item["price_paise"])
                else:
                    if int(line["quantity"]) > int(item["stock"]):
                        msg = f"only {item['stock']} of {item['name']} left in stock"
                        raise ToolRejected(Rejection.OUT_OF_STOCK, msg)
                    if behaviour.price_change_bps and not item["price_changed"]:
                        diff += self._set(
                            "product",
                            item,
                            price_paise=raised(
                                int(item["price_paise"]), behaviour.price_change_bps
                            ),
                            price_changed=1,
                        )
                    price = int(item["price_paise"])
                    diff += self._set(
                        "product", item, stock=int(item["stock"]) - int(line["quantity"])
                    )
                items.append(
                    {
                        "item_id": item["id"],
                        "name": item["name"],
                        "variant": line["variant"],
                        "quantity": line["quantity"],
                        "cart_unit_price_paise": line["unit_price_paise"],
                        "unit_price_paise": price,
                    }
                )
            priced = [
                {**line, "unit_price_paise": item["unit_price_paise"]}
                for line, item in zip(lines, items, strict=True)
            ]
            cart = self._one("SELECT offer_code FROM cart WHERE merchant_id = ?", (merchant_id,))
            code = None if cart is None else cart["offer_code"]
            discount, dropped = 0, None
            if code is not None:
                discount, problem = self._discount(merchant_id, str(code), priced, now)
                if problem is not None:
                    discount, dropped, code = 0, f"offer {code} no longer applies ({problem})", None
            subtotal = sum(int(i["unit_price_paise"]) * int(i["quantity"]) for i in items)
            order: Row = {
                "id": self._next_id("ord", "orders"),
                "merchant_id": merchant_id,
                "kind": (OrderKind.SUBSCRIPTION if plan else OrderKind.PURCHASE).value,
                "status": OrderStatus.CREATED.value,
                "subtotal_paise": subtotal,
                "discount_paise": discount,
                "total_paise": subtotal - discount,
                "offer_code": code,
                "plan_id": None if plan is None else plan["id"],
                "subscription_id": None,
                "created_at": iso(now),
                "pay_by": iso(
                    now + dt.timedelta(seconds=config.settings.order_payment_window_seconds)
                ),
                "paid_at": None,
                "payment_id": None,
                "ships_wrong_item": 0,
                "expected_delivery_at": None,
                "shipped_at": None,
                "delivered_at": None,
                "delivered_item": None,
                "cancel_reason": None,
            }
            self._insert("orders", order)
            diff.append(self._created("orders", order))
            for item in items:
                self._insert("order_item", {"order_id": order["id"], **item})
            self._db.execute("DELETE FROM cart_line WHERE merchant_id = ?", (merchant_id,))
            self._db.execute("DELETE FROM cart WHERE merchant_id = ?", (merchant_id,))
            diff.append(DiffEntry(op="delete", path=f"commerce/cart/{merchant_id}"))
            notices = [self._document(merchant, order, "invoice", now)]
            scheduled: list[ScheduledEvent] = []
            if order["total_paise"] == 0:
                more, notes, events = self._mark_paid(order, None, None, now)
                diff += more
                notices += notes
                scheduled += events
            else:
                scheduled.append(
                    ScheduledEvent(
                        at=parse(str(order["pay_by"])),
                        name=EVENT_ORDER_EXPIRY,
                        payload={"order_id": order["id"]},
                    )
                )
            view = self._order_view(order)
        request = None
        if view["status"] == OrderStatus.CREATED:
            request = {
                "order_id": order["id"],
                "payee": merchant["handle"],
                "amount_paise": order["total_paise"],
                "pay_by": order["pay_by"],
                "mandate_required": None
                if plan is None
                else {
                    "type": "recurring",
                    "frequency": plan["frequency"],
                    "min_max_amount_paise": plan["price_paise"],
                },
            }
        return Result(
            body={
                "order": view,
                "payment_request": request,
                "offer_dropped": dropped,
                "notices": notices,
            },
            state_diff=diff,
            scheduled=scheduled,
            created=True,
        )

    def _document(self, merchant: Row, order: Row, kind: str, now: dt.datetime) -> Row:
        """Issue an invoice or receipt and return the notice that carries it."""
        count = int(self._scalar("SELECT COUNT(*) FROM document")) + 1
        number = f"{'INV' if kind == 'invoice' else 'RCT'}-{count:05d}"
        if kind == "invoice":
            items = self.store.query_all(
                "SELECT * FROM order_item WHERE order_id = ? ORDER BY id", (order["id"],)
            )
            text = pages.invoice(number, merchant, order, items)
            subject = f"Invoice {number} for order {order['id']}"
        else:
            text = pages.receipt(number, merchant, order, order["payment_id"])
            subject = f"Receipt {number} for order {order['id']}"
        self._insert(
            "document",
            {
                "id": number,
                "order_id": order["id"],
                "kind": kind,
                "text": text,
                "created_at": iso(now),
            },
        )
        return notice(merchant, subject, text, str(order["id"]))

    # -------------------------------------------------------------- payments
    def payable(
        self, order_id: str, payee: str, amount: int, mandate_id: str | None, now: dt.datetime
    ) -> Row:
        """Check a payment would pay this order, before it is sent. Reads only."""
        self._config()
        order = self._order(order_id)
        merchant = self._merchant(str(order["merchant_id"]))
        status = OrderStatus(order["status"])
        if status is OrderStatus.CANCELLED:
            msg = f"order {order_id} was cancelled ({order['cancel_reason']})"
            raise ToolRejected(Rejection.ORDER_NOT_PAYABLE, msg)
        if status is not OrderStatus.CREATED:
            msg = f"order {order_id} is already paid"
            raise ToolRejected(Rejection.ORDER_ALREADY_PAID, msg)
        if now >= parse(str(order["pay_by"])):
            msg = f"the payment window for order {order_id} closed at {order['pay_by']}"
            raise ToolRejected(Rejection.ORDER_NOT_PAYABLE, msg)
        if payee != merchant["handle"] or amount != order["total_paise"]:
            msg = (
                f"order {order_id} must be paid {order['total_paise']} paise to "
                f"{merchant['handle']}, not {amount} to {payee}"
            )
            raise ToolRejected(Rejection.ORDER_MISMATCH, msg)
        if order["kind"] == OrderKind.SUBSCRIPTION and mandate_id is None:
            msg = "a subscription is paid with a recurring mandate to the merchant; give mandate_id"
            raise ToolRejected(Rejection.MANDATE_REQUIRED, msg)
        return self._order_view(order)

    def link_payment(self, order_id: str, link: PaymentLink, now: dt.datetime) -> Result:
        """Record a rails payment made for an order, and pay the order if it succeeded."""
        with self._transaction():
            self._config()
            order = self._order(order_id)
            existing = self._one("SELECT * FROM order_payment WHERE id = ?", (link.payment_id,))
            diff: list[DiffEntry] = []
            if existing is None:
                row = {
                    "id": link.payment_id,
                    "order_id": order_id,
                    "amount_paise": link.amount_paise,
                    "status": link.status,
                    "mandate_id": link.mandate_id,
                    "linked_at": iso(now),
                }
                self._insert("order_payment", row)
                diff.append(self._created("order_payment", row))
            else:
                diff += self._set("order_payment", existing, status=link.status)
            more, notices, scheduled = self._after_payment(
                order, link.payment_id, link.status, link.mandate_id, now
            )
        return Result(
            body={"order": self._order_view(self._order(order_id)), "notices": notices},
            state_diff=diff + more,
            scheduled=scheduled,
        )

    def payment_status(self, update: PaymentStatusUpdate, now: dt.datetime) -> Result:
        """A timed-out payment settled. Nothing happens if it was not for an order."""
        with self._transaction():
            self._config()
            link = self._one("SELECT * FROM order_payment WHERE id = ?", (update.payment_id,))
            if link is None:
                return Result(body={"order": None, "notices": []})
            diff = self._set("order_payment", link, status=update.status)
            order = self._order(str(link["order_id"]))
            more, notices, scheduled = self._after_payment(
                order, update.payment_id, update.status, link["mandate_id"], now
            )
        return Result(
            body={
                "order": self._order_view(self._order(str(link["order_id"]))),
                "notices": notices,
            },
            state_diff=diff + more,
            scheduled=scheduled,
        )

    def _after_payment(
        self, order: Row, payment_id: str, status: str, mandate_id: str | None, now: dt.datetime
    ) -> tuple[list[DiffEntry], list[Row], list[ScheduledEvent]]:
        if order["status"] != OrderStatus.CREATED:
            return (
                [],
                [],
                [],
            )  # an extra payment for a paid order: a duplicate, left for reconciliation
        if status == "success":
            return self._mark_paid(order, payment_id, mandate_id, now)
        if status in ("failed", "reversed"):
            open_payments = self._open_payments(str(order["id"]))
            if order["kind"] == OrderKind.RENEWAL:
                return self._renewal_failed(order, now)
            if not open_payments and now >= parse(str(order["pay_by"])):
                return self._cancel(order, "payment_window_closed", now)
        return [], [], []

    def _mark_paid(
        self, order: Row, payment_id: str | None, mandate_id: str | None, now: dt.datetime
    ) -> tuple[list[DiffEntry], list[Row], list[ScheduledEvent]]:
        merchant = self._merchant(str(order["merchant_id"]))
        behaviour = self._behaviour(merchant)
        diff = self._set(
            "orders", order, status=OrderStatus.PAID.value, paid_at=iso(now), payment_id=payment_id
        )
        notices = [self._document(merchant, order, "receipt", now)]
        scheduled: list[ScheduledEvent] = []
        kind = OrderKind(order["kind"])
        if kind is OrderKind.PURCHASE:
            count = int(merchant["paid_orders"]) + 1
            diff += self._set("merchant", merchant, paid_orders=count)
            wrong = count in behaviour.wrong_item_on_orders or (
                behaviour.wrong_item_rate > 0
                and self._rng.child(f"wrong_item/{merchant['id']}/{count}").chance(
                    behaviour.wrong_item_rate
                )
            )
            expected = now + dt.timedelta(seconds=behaviour.promised_delivery_seconds)
            diff += self._set(
                "orders", order, ships_wrong_item=int(wrong), expected_delivery_at=iso(expected)
            )
            for name, after in (
                (EVENT_SHIP, behaviour.ship_after_seconds),
                (EVENT_DELIVER, behaviour.deliver_after_seconds),
            ):
                scheduled.append(
                    ScheduledEvent(
                        at=now + dt.timedelta(seconds=after),
                        name=name,
                        payload={"order_id": order["id"]},
                    )
                )
        elif kind is OrderKind.SUBSCRIPTION:
            diff += self._set(
                "orders", order, status=OrderStatus.DELIVERED.value, delivered_at=iso(now)
            )
            plan = self._one("SELECT * FROM plan WHERE id = ?", (order["plan_id"],))
            assert plan is not None and mandate_id is not None  # payable() insists on both
            at = next_period(now, Frequency(plan["frequency"]))
            sub: Row = {
                "id": self._next_id("sub", "subscription"),
                "merchant_id": merchant["id"],
                "plan_id": plan["id"],
                "mandate_id": mandate_id,
                "status": SubscriptionStatus.ACTIVE.value,
                "started_at": iso(now),
                "next_billing_at": iso(at),
                "pending_plan_id": None,
                "renewals": 0,
            }
            self._insert("subscription", sub)
            diff.append(self._created("subscription", sub))
            diff += self._set("orders", order, subscription_id=sub["id"])
            scheduled.append(
                ScheduledEvent(at=at, name=EVENT_RENEWAL, payload={"subscription_id": sub["id"]})
            )
        else:
            diff += self._set(
                "orders", order, status=OrderStatus.DELIVERED.value, delivered_at=iso(now)
            )
            sub_row = self._one(
                "SELECT * FROM subscription WHERE id = ?", (order["subscription_id"],)
            )
            assert sub_row is not None
            plan = self._one("SELECT * FROM plan WHERE id = ?", (order["plan_id"],))
            assert plan is not None
            at = next_period(now, Frequency(plan["frequency"]))
            diff += self._set("subscription", sub_row, next_billing_at=iso(at))
            scheduled.append(
                ScheduledEvent(
                    at=at, name=EVENT_RENEWAL, payload={"subscription_id": sub_row["id"]}
                )
            )
        return diff, notices, scheduled

    def _cancel(
        self, order: Row, reason: str, now: dt.datetime
    ) -> tuple[list[DiffEntry], list[Row], list[ScheduledEvent]]:
        merchant = self._merchant(str(order["merchant_id"]))
        diff = self._set("orders", order, status=OrderStatus.CANCELLED.value, cancel_reason=reason)
        items = self.store.query_all("SELECT * FROM order_item WHERE order_id = ?", (order["id"],))
        for item in items:
            product = self._one("SELECT * FROM product WHERE id = ?", (item["item_id"],))
            if product is not None:
                diff += self._set(
                    "product", product, stock=int(product["stock"]) + int(item["quantity"])
                )
        body = f"Order {order['id']} was cancelled: {reason.replace('_', ' ')}."
        return (
            diff,
            [notice(merchant, f"Order {order['id']} cancelled", body, str(order["id"]))],
            [],
        )

    def _renewal_failed(
        self, order: Row, now: dt.datetime
    ) -> tuple[list[DiffEntry], list[Row], list[ScheduledEvent]]:
        diff, notices, _ = self._cancel(order, "renewal_payment_declined", now)
        sub = self._one("SELECT * FROM subscription WHERE id = ?", (order["subscription_id"],))
        assert sub is not None
        diff += self._set(
            "subscription", sub, status=SubscriptionStatus.PAST_DUE.value, next_billing_at=None
        )
        merchant = self._merchant(str(order["merchant_id"]))
        body = (
            f"We could not collect {pages.money(int(order['total_paise']))} for subscription "
            f"{sub['id']} through mandate {sub['mandate_id']}. The subscription is past due."
        )
        notices.append(
            notice(merchant, f"Subscription {sub['id']} payment failed", body, str(order["id"]))
        )
        return diff, notices, []

    # ---------------------------------------------------------- subscriptions
    def change_subscription(
        self, subscription_id: str, change: SubscriptionChange, now: dt.datetime
    ) -> Result:
        """Change plan from the next renewal, or cancel now. A cancelled one stays cancelled."""
        with self._transaction():
            self._config()
            sub = self._one("SELECT * FROM subscription WHERE id = ?", (subscription_id,))
            if sub is None:
                msg = f"no subscription {subscription_id!r}"
                raise ToolRejected(Rejection.SUBSCRIPTION_NOT_FOUND, msg)
            if sub["status"] == SubscriptionStatus.CANCELLED:
                msg = f"subscription {subscription_id} is already cancelled"
                raise ToolRejected(Rejection.INVALID_TRANSITION, msg)
            if change.action == "cancel":
                diff = self._set(
                    "subscription",
                    sub,
                    status=SubscriptionStatus.CANCELLED.value,
                    next_billing_at=None,
                    pending_plan_id=None,
                )
            else:
                plan = self._one(
                    "SELECT * FROM plan WHERE id = ? AND merchant_id = ?",
                    (change.plan_id, sub["merchant_id"]),
                )
                if plan is None:
                    msg = f"{sub['merchant_id']!r} has no plan {change.plan_id!r}"
                    raise ToolRejected(Rejection.PLAN_NOT_FOUND, msg)
                if sub["status"] != SubscriptionStatus.ACTIVE:
                    msg = (
                        f"subscription {subscription_id} is {sub['status']}; "
                        "only an active one can change plan"
                    )
                    raise ToolRejected(Rejection.INVALID_TRANSITION, msg)
                pending = None if plan["id"] == sub["plan_id"] else plan["id"]
                diff = self._set("subscription", sub, pending_plan_id=pending)
        return Result(body={"subscription": dict(sub)}, state_diff=diff)

    # ----------------------------------------------------------------- events
    def apply_event(self, name: str, payload: dict[str, Any], now: dt.datetime) -> Result:
        """Apply something a merchant does on its own at a scheduled time."""
        with self._transaction():
            self._config()
            diff: list[DiffEntry] = []
            notices: list[Row] = []
            charge: Row | None = None
            if name == EVENT_ORDER_EXPIRY:
                order = self._order(str(payload.get("order_id")))
                still_open = self._open_payments(str(order["id"]))
                if order["status"] == OrderStatus.CREATED and not still_open:
                    diff, notices, _ = self._cancel(order, "payment_window_closed", now)
            elif name == EVENT_SHIP:
                diff, notices = self._ship(self._order(str(payload.get("order_id"))), now)
            elif name == EVENT_DELIVER:
                diff, notices = self._deliver(self._order(str(payload.get("order_id"))), now)
            elif name == EVENT_RENEWAL:
                diff, charge = self._renew(str(payload.get("subscription_id")), now)
            else:
                msg = f"commerce does not handle event {name!r}"
                raise ToolRejected(Rejection.UNKNOWN_EVENT, msg)
        return Result(body={"event": name, "notices": notices, "charge": charge}, state_diff=diff)

    def _ship(self, order: Row, now: dt.datetime) -> tuple[list[DiffEntry], list[Row]]:
        if order["status"] != OrderStatus.PAID:
            return [], []
        items = self.store.query_all(
            "SELECT * FROM order_item WHERE order_id = ? ORDER BY id", (order["id"],)
        )
        sent = [
            self._shipped_item(item, bool(order["ships_wrong_item"]) and index == 0)
            for index, item in enumerate(items)
        ]
        diff = self._set(
            "orders",
            order,
            status=OrderStatus.SHIPPED.value,
            shipped_at=iso(now),
            delivered_item="; ".join(sent),
        )
        merchant = self._merchant(str(order["merchant_id"]))
        body = (
            f"Order {order['id']} has shipped: {'; '.join(sent)}. "
            f"Expected by {order['expected_delivery_at']}."
        )
        return diff, [notice(merchant, f"Order {order['id']} shipped", body, str(order["id"]))]

    def _shipped_item(self, item: Row, wrong: bool) -> str:
        """What actually goes in the box. A wrong item is another variant, or another product."""
        label = f"{item['quantity']} x {item['name']}" + (
            f" ({item['variant']})" if item["variant"] else ""
        )
        if not wrong:
            return label
        product = self._one("SELECT * FROM product WHERE id = ?", (item["item_id"],))
        variants = (
            []
            if product is None
            else [v for v in json.loads(str(product["variants"])) if v != item["variant"]]
        )
        if variants:
            return f"{item['quantity']} x {item['name']} ({variants[0]})"
        return f"{item['quantity']} x an item that is not {item['name']}"

    def _deliver(self, order: Row, now: dt.datetime) -> tuple[list[DiffEntry], list[Row]]:
        if order["status"] != OrderStatus.SHIPPED:
            return [], []
        wrong = bool(order["ships_wrong_item"])
        status = OrderStatus.WRONG_ITEM if wrong else OrderStatus.DELIVERED
        diff = self._set("orders", order, status=status.value, delivered_at=iso(now))
        merchant = self._merchant(str(order["merchant_id"]))
        body = f"Order {order['id']} was delivered. In the parcel: {order['delivered_item']}."
        return diff, [notice(merchant, f"Order {order['id']} delivered", body, str(order["id"]))]

    def _renew(self, subscription_id: str, now: dt.datetime) -> tuple[list[DiffEntry], Row | None]:
        sub = self._one("SELECT * FROM subscription WHERE id = ?", (subscription_id,))
        if sub is None or sub["status"] != SubscriptionStatus.ACTIVE:
            return [], None
        diff: list[DiffEntry] = []
        if sub["pending_plan_id"] is not None:
            diff += self._set(
                "subscription", sub, plan_id=sub["pending_plan_id"], pending_plan_id=None
            )
        plan = self._one("SELECT * FROM plan WHERE id = ?", (sub["plan_id"],))
        assert plan is not None
        renewals = int(sub["renewals"]) + 1
        diff += self._set("subscription", sub, renewals=renewals)
        price = int(plan["price_paise"])
        window = self._config().settings.order_payment_window_seconds
        order: Row = {
            "id": self._next_id("ord", "orders"),
            "merchant_id": sub["merchant_id"],
            "kind": OrderKind.RENEWAL.value,
            "status": OrderStatus.CREATED.value,
            "subtotal_paise": price,
            "discount_paise": 0,
            "total_paise": price,
            "offer_code": None,
            "plan_id": plan["id"],
            "subscription_id": sub["id"],
            "created_at": iso(now),
            "pay_by": iso(now + dt.timedelta(seconds=window)),
            "paid_at": None,
            "payment_id": None,
            "ships_wrong_item": 0,
            "expected_delivery_at": None,
            "shipped_at": None,
            "delivered_at": None,
            "delivered_item": None,
            "cancel_reason": None,
        }
        self._insert("orders", order)
        self._insert(
            "order_item",
            {
                "order_id": order["id"],
                "item_id": plan["id"],
                "name": plan["name"],
                "variant": "",
                "quantity": 1,
                "cart_unit_price_paise": price,
                "unit_price_paise": price,
            },
        )
        diff.append(self._created("orders", order))
        merchant = self._merchant(str(sub["merchant_id"]))
        charge = {
            "order_id": order["id"],
            "payer": self._user(),
            "payee": merchant["handle"],
            "amount_paise": price,
            "mandate_id": sub["mandate_id"],
            "idempotency_key": f"renewal-{sub['id']}-{renewals}",
            "note": f"{plan['name']} renewal {renewals}",
        }
        return diff, charge

    # ---------------------------------------------------------------- reading
    def search(
        self,
        query: str | None,
        merchant_id: str | None,
        category: str | None,
        max_price_paise: int | None,
    ) -> list[Row]:
        """Products and plans matching every filter given, at today's shown prices."""
        self._config()
        found: list[Row] = []
        needle = (query or "").strip().lower()
        merchants = {row["id"]: row for row in self.store.query_all("SELECT * FROM merchant")}
        products = self.store.query_all("SELECT * FROM product ORDER BY merchant_id, id")
        plans = self.store.query_all("SELECT * FROM plan ORDER BY merchant_id, id")
        rows = [("product", row) for row in products] + [("plan", row) for row in plans]
        for kind, row in rows:
            merchant = merchants[row["merchant_id"]]
            category_of = row["category"] if kind == "product" else merchant["category"]
            text = f"{row['name']} {row['description']} {category_of} {merchant['name']}".lower()
            if needle and not all(word in text for word in needle.split()):
                continue
            if merchant_id is not None and row["merchant_id"] != merchant_id:
                continue
            if category is not None and category_of.lower() != category.lower():
                continue
            if max_price_paise is not None and int(row["price_paise"]) > max_price_paise:
                continue
            entry: Row = {
                "kind": kind,
                "product_id": row["id"],
                "name": row["name"],
                "merchant_id": row["merchant_id"],
                "merchant_name": merchant["name"],
                "category": category_of,
                "price_paise": row["price_paise"],
            }
            if kind == "product":
                entry.update(stock=row["stock"], variants=json.loads(str(row["variants"])))
            else:
                entry["frequency"] = row["frequency"]
            found.append(entry)
        return found

    def page(self, merchant_id: str, name: str) -> Row:
        """A merchant page as text, with the task's extra content applied last."""
        self._config()
        merchant = self._merchant(merchant_id)
        behaviour = json.loads(str(merchant["behaviour"]))
        overrides = json.loads(str(merchant["pages"]))
        products = self.store.query_all(
            "SELECT * FROM product WHERE merchant_id = ? ORDER BY id", (merchant_id,)
        )
        plans = self.store.query_all(
            "SELECT * FROM plan WHERE merchant_id = ? ORDER BY id", (merchant_id,)
        )
        if name in overrides:
            text = str(overrides[name])
        elif name == "home":
            text = pages.home(merchant, products, plans)
        elif name == "terms":
            text = pages.terms(merchant, behaviour)
        elif name == "refund-policy":
            text = pages.refund_policy(merchant, behaviour, str(merchant["profile"]))
        elif name == "plans":
            text = pages.plans(merchant, plans)
        elif name == "account":
            orders = self.store.query_all(
                "SELECT * FROM orders WHERE merchant_id = ? ORDER BY rowid", (merchant_id,)
            )
            subs = self.store.query_all(
                "SELECT * FROM subscription WHERE merchant_id = ? ORDER BY id", (merchant_id,)
            )
            text = pages.account(merchant, orders, subs)
        elif name.startswith("product/"):
            product = next(
                (row for row in products if row["id"] == name.removeprefix("product/")), None
            )
            if product is None:
                msg = f"{merchant_id!r} has no product page {name!r}"
                raise ToolRejected(Rejection.PAGE_NOT_FOUND, msg)
            text = pages.product(merchant, product)
        else:
            msg = f"no page {name!r}; pages are {list(PAGES)} and product/<product_id>"
            raise ToolRejected(Rejection.PAGE_NOT_FOUND, msg)
        extras = self.store.query_all(
            "SELECT * FROM page_content WHERE merchant_id = ? AND page = ? ORDER BY id",
            (merchant_id, name),
        )
        return {"merchant_id": merchant_id, "page": name, "text": pages.apply_content(text, extras)}

    def _order_view(self, order: Row) -> Row:
        items = self.store.query_all(
            "SELECT item_id, name, variant, quantity, unit_price_paise FROM order_item"
            " WHERE order_id = ? ORDER BY id",
            (order["id"],),
        )
        payments = self.store.query_all(
            "SELECT id AS payment_id, amount_paise, status, mandate_id FROM order_payment"
            " WHERE order_id = ? ORDER BY linked_at, id",
            (order["id"],),
        )
        documents = self.store.query_all(
            "SELECT id, kind, text FROM document WHERE order_id = ? ORDER BY id", (order["id"],)
        )
        hidden = {"ships_wrong_item"}
        return {
            **{key: value for key, value in order.items() if key not in hidden},
            "items": [{**item, "variant": item["variant"] or None} for item in items],
            "payments": payments,
            "documents": documents,
        }

    def order(self, order_id: str) -> Row:
        """One order with its items, linked payments, invoice and receipt."""
        self._config()
        return self._order_view(self._order(order_id))

    def order_for_payment(self, payment_id: str) -> Row:
        """Which order a rails payment was made for, if any."""
        self._config()
        link = self._one("SELECT order_id FROM order_payment WHERE id = ?", (payment_id,))
        return {"payment_id": payment_id, "order_id": None if link is None else link["order_id"]}

    def merchant(self, merchant_id: str) -> Row:
        """A merchant's public details."""
        self._config()
        row = self._merchant(merchant_id)
        return {key: row[key] for key in ("id", "name", "handle", "category")}

    def merchant_by_handle(self, handle: str) -> Row:
        """The merchant behind a payee handle, for disputes on payments made outside an order."""
        self._config()
        row = self._one("SELECT id FROM merchant WHERE handle = ?", (handle,))
        if row is None:
            msg = f"{handle!r} is not a merchant in this world"
            raise ToolRejected(Rejection.MERCHANT_NOT_FOUND, msg)
        return self.merchant(str(row["id"]))

    # ----------------------------------------------------------------- admin
    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; commerce has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(Rejection.UNKNOWN_RESOURCE, msg)
        if resource == "state":
            return {name: self.admin(name) for name in ADMIN_RESOURCES[1:]}
        tables = {
            "merchants": "SELECT * FROM merchant ORDER BY id",
            "products": "SELECT * FROM product ORDER BY id",
            "plans": "SELECT * FROM plan ORDER BY id",
            "offers": "SELECT * FROM offer ORDER BY id",
            "carts": "SELECT * FROM cart_line ORDER BY merchant_id, item_id, variant",
            "orders": "SELECT * FROM orders ORDER BY rowid",
            "order_items": "SELECT * FROM order_item ORDER BY id",
            "order_payments": "SELECT * FROM order_payment ORDER BY rowid",
            "documents": "SELECT * FROM document ORDER BY id",
            "subscriptions": "SELECT * FROM subscription ORDER BY rowid",
        }
        return self.store.query_all(tables[resource])


__all__ = [
    "EVENT_DELIVER",
    "EVENT_ORDER_EXPIRY",
    "EVENT_RENEWAL",
    "EVENT_SHIP",
    "SCHEMA",
    "Commerce",
    "next_period",
    "raised",
]
