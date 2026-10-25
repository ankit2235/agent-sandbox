"""What a task may configure in commerce, and what callers may send it.

Money is integer paise, as in the rails (decision 21).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, model_validator

from kit.types import Strict

from ...merchants import MerchantSeed, Slug
from ..rails.models import Frequency, Handle

Paise = Annotated[StrictInt, Field(ge=0)]
PositivePaise = Annotated[StrictInt, Field(gt=0)]

PAGES = ("home", "terms", "refund-policy", "plans", "account")
"""Fixed page names; a product page is ``product/<product_id>``."""


class OrderStatus(StrEnum):
    """The order states the phase prompt names."""

    CREATED = "created"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    WRONG_ITEM = "wrong_item"
    CANCELLED = "cancelled"


class OrderKind(StrEnum):
    """A purchase from a cart, the first charge of a subscription, or a renewal."""

    PURCHASE = "purchase"
    SUBSCRIPTION = "subscription"
    RENEWAL = "renewal"


class SubscriptionStatus(StrEnum):
    """``past_due``: a renewal was declined, so billing stopped."""

    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"


class Rejection(StrEnum):
    """Why commerce refused a request. Nothing is recorded."""

    INVALID_REQUEST = "invalid_request"
    MERCHANT_NOT_FOUND = "merchant_not_found"
    PRODUCT_NOT_FOUND = "product_not_found"
    VARIANT_REQUIRED = "variant_required"
    VARIANT_NOT_FOUND = "variant_not_found"
    OUT_OF_STOCK = "out_of_stock"
    PLAN_IN_CART = "plan_in_cart"
    CART_EMPTY = "cart_empty"
    OFFER_NOT_FOUND = "offer_not_found"
    OFFER_EXPIRED = "offer_expired"
    OFFER_CONDITIONS_NOT_MET = "offer_conditions_not_met"
    ORDER_NOT_FOUND = "order_not_found"
    ORDER_MISMATCH = "order_mismatch"
    ORDER_NOT_PAYABLE = "order_not_payable"
    ORDER_ALREADY_PAID = "order_already_paid"
    MANDATE_REQUIRED = "mandate_required"
    SUBSCRIPTION_NOT_FOUND = "subscription_not_found"
    PLAN_NOT_FOUND = "plan_not_found"
    INVALID_TRANSITION = "invalid_transition"
    PAGE_NOT_FOUND = "page_not_found"
    ALREADY_LOADED = "already_loaded"
    UNKNOWN_EVENT = "unknown_event"
    UNKNOWN_RESOURCE = "unknown_resource"
    DEAL_NOT_FOUND = "deal_not_found"
    DEAL_NOT_OPEN = "deal_not_open"
    DEAL_NOT_AGREED = "deal_not_agreed"
    NO_STANDING_OFFER = "no_standing_offer"
    INVOICE_NOT_FOUND = "invoice_not_found"
    INVOICE_NOT_OPEN = "invoice_not_open"
    LINE_NOT_FOUND = "line_not_found"


# ---------------------------------------------------------------- task config
class ProductSeed(Strict):
    """Something a merchant sells."""

    id: Slug
    merchant_id: Slug
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=40)
    price_paise: PositivePaise
    stock: Annotated[StrictInt, Field(ge=0)] = 10
    variants: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(default_factory=list)
    description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _unique_variants(self) -> ProductSeed:
        if len(set(self.variants)) != len(self.variants):
            msg = f"product {self.id!r}: variants must be unique"
            raise ValueError(msg)
        return self


class PlanSeed(Strict):
    """A subscription plan, billed through a recurring mandate."""

    id: Slug
    merchant_id: Slug
    name: str = Field(min_length=1, max_length=80)
    price_paise: PositivePaise
    frequency: Frequency
    description: str = Field(default="", max_length=500)


class OfferSeed(Strict):
    """A discount code. Exactly one of ``percent_off`` and ``flat_off_paise``."""

    code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,31}$")]
    merchant_id: Slug
    percent_off: Annotated[StrictInt, Field(gt=0, le=100)] | None = None
    flat_off_paise: PositivePaise | None = None
    max_discount_paise: PositivePaise | None = None
    min_cart_paise: Paise = 0
    product_ids: list[Slug] = Field(default_factory=list)
    """If set, the offer only discounts these products, and needs one in the cart."""
    first_order_only: bool = False
    expires_after_seconds: StrictInt | None = None
    """Seconds after the task loads; zero or less means it has already expired."""

    @model_validator(mode="after")
    def _one_kind(self) -> OfferSeed:
        if (self.percent_off is None) == (self.flat_off_paise is None):
            msg = f"offer {self.code!r}: give exactly one of percent_off and flat_off_paise"
            raise ValueError(msg)
        return self


class SubscriptionSeed(Strict):
    """A subscription the user already has, billed through a mandate in the rails."""

    id: Slug
    plan_id: Slug
    mandate_id: Slug
    next_billing_after_seconds: Annotated[StrictInt, Field(gt=0)]


class PageContent(Strict):
    """Extra text on a merchant page. The extension point for Phase 5's stress content."""

    merchant_id: Slug
    page: str = Field(min_length=1, max_length=80)
    mode: Literal["append", "prepend", "replace"] = "append"
    text: str = Field(min_length=1, max_length=4_000)


class DealSeed(Strict):
    """A deal between the user and a seller agent, for multi-agent tasks (Phase 9).

    Open by default: the price is still to be agreed. With ``agreed_price_paise`` the price
    was agreed before the task starts (a quote), and the seller invoices it.
    """

    id: Slug
    merchant_id: Slug
    product_id: Slug
    agreed_price_paise: PositivePaise | None = None
    """The total the user and the seller agreed before the task, if they did."""
    opening_price_paise: PositivePaise | None = None
    """The seller's standing offer when the task starts, on an open deal."""
    opening_message: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def _open_or_agreed(self) -> DealSeed:
        if self.agreed_price_paise is not None and self.opening_price_paise is not None:
            msg = f"deal {self.id!r}: an agreed deal has no opening offer"
            raise ValueError(msg)
        return self


class CommerceSettings(Strict):
    """Rules no public source fixes. See docs/DECISIONS.md."""

    order_payment_window_seconds: Annotated[StrictInt, Field(gt=0)] = 900
    """How long an unpaid order waits before it is cancelled. Invented default: 15 minutes."""


class CommerceConfig(Strict):
    """The ``commerce`` part of a payments task's ``initial_state``."""

    merchants: list[MerchantSeed] = Field(default_factory=list)
    products: list[ProductSeed] = Field(default_factory=list)
    plans: list[PlanSeed] = Field(default_factory=list)
    offers: list[OfferSeed] = Field(default_factory=list)
    subscriptions: list[SubscriptionSeed] = Field(default_factory=list)
    page_content: list[PageContent] = Field(default_factory=list)
    settings: CommerceSettings = Field(default_factory=CommerceSettings)
    deals: list[DealSeed] = Field(default_factory=list)
    """Deals with a seller agent (multi-agent tasks only)."""

    @model_validator(mode="after")
    def _references(self) -> CommerceConfig:
        merchant_ids = [merchant.id for merchant in self.merchants]
        _unique("merchant ids", merchant_ids)
        _unique("merchant handles", [merchant.handle for merchant in self.merchants])
        item_ids = [product.id for product in self.products] + [plan.id for plan in self.plans]
        _unique("product and plan ids", item_ids)
        _unique("offer codes", [(offer.merchant_id, offer.code) for offer in self.offers])
        _unique("subscription ids", [sub.id for sub in self.subscriptions])
        _unique("deal ids", [deal.id for deal in self.deals])
        for merchant in self.merchants:
            merchant.resolved()
        known = set(merchant_ids)
        used = [
            *(product.merchant_id for product in self.products),
            *(plan.merchant_id for plan in self.plans),
            *(offer.merchant_id for offer in self.offers),
            *(content.merchant_id for content in self.page_content),
        ]
        unknown = sorted(set(used) - known)
        if unknown:
            msg = f"unknown merchants referenced: {unknown}"
            raise ValueError(msg)
        products = {product.id: product.merchant_id for product in self.products}
        for offer in self.offers:
            wrong = [pid for pid in offer.product_ids if products.get(pid) != offer.merchant_id]
            if wrong:
                msg = f"offer {offer.code!r} names products its merchant does not sell: {wrong}"
                raise ValueError(msg)
        plans = {plan.id for plan in self.plans}
        missing = sorted({sub.plan_id for sub in self.subscriptions} - plans)
        if missing:
            msg = f"subscriptions name unknown plans: {missing}"
            raise ValueError(msg)
        for deal in self.deals:
            if products.get(deal.product_id) != deal.merchant_id:
                msg = f"deal {deal.id!r}: {deal.merchant_id!r} does not sell {deal.product_id!r}"
                raise ValueError(msg)
        return self

    def stored(self) -> str:
        """The config as commerce keeps it.

        Without ``deals`` when there are none, so a task without deals is stored exactly as
        before deals existed.
        """
        return self.model_dump_json(exclude=None if self.deals else {"deals"})


def _unique(what: str, values: list[Any]) -> None:
    if len(set(values)) != len(values):
        msg = f"{what} must be unique"
        raise ValueError(msg)


# ------------------------------------------------------------------- requests
class LoadRequest(Strict):
    """What the episode loads: the task's commerce config and the user it acts for."""

    user: Handle
    config: CommerceConfig


class CartLine(Strict):
    """Set one line of a cart. Quantity 0 removes it."""

    product_id: Slug
    variant: str | None = Field(default=None, max_length=40)
    quantity: Annotated[StrictInt, Field(ge=0, le=10)]


class OfferRequest(Strict):
    """Apply a discount code to a cart."""

    code: str = Field(min_length=1, max_length=32)


class PaymentLink(Strict):
    """A rails payment made for an order, as the rails reported it."""

    payment_id: str = Field(min_length=1, max_length=64)
    amount_paise: StrictInt
    status: Literal["pending", "success", "failed", "reversed"]
    mandate_id: str | None = None


class PaymentStatusUpdate(Strict):
    """A payment's final status, delivered when a timed-out payment settles."""

    payment_id: str = Field(min_length=1, max_length=64)
    status: Literal["pending", "success", "failed", "reversed"]


class SubscriptionChange(Strict):
    """Change a subscription's plan from the next renewal, or cancel it."""

    action: Literal["change_plan", "cancel"]
    plan_id: Slug | None = None

    @model_validator(mode="after")
    def _plan_for_change(self) -> SubscriptionChange:
        if (self.action == "change_plan") != (self.plan_id is not None):
            msg = "plan_id is required to change plan, and only then"
            raise ValueError(msg)
        return self


Sender = Literal["buyer", "seller"]


class DealMove(Strict):
    """An offer, an acceptance or a message on a deal, from either side."""

    sender: Sender
    merchant_id: Slug | None = None
    """For the seller's moves: the merchant it acts for. A deal of another merchant is unknown."""
    price_paise: PositivePaise | None = None
    text: str = Field(default="", max_length=2_000)


class InvoiceLine(Strict):
    """A line the seller adds to an invoice beside the item: delivery, a fee."""

    label: str = Field(min_length=1, max_length=60)
    amount_paise: PositivePaise


class InvoiceCreate(Strict):
    """The seller invoices an agreed deal, optionally to collect under a mandate."""

    merchant_id: Slug
    item_price_paise: PositivePaise
    fees: list[InvoiceLine] = Field(default_factory=list, max_length=5)
    mandate_id: str | None = Field(default=None, max_length=64)
    """Collect the invoice under this mandate after the pre-debit notice; checked by the tool."""
    replaces: str | None = Field(default=None, max_length=64)
    """An open invoice of the same deal this one replaces; it is withdrawn."""
    text: str = Field(default="", max_length=2_000)


class InvoiceWithdraw(Strict):
    """The seller withdraws one of its unpaid invoices."""

    merchant_id: Slug
    text: str = Field(default="", max_length=2_000)


class InvoiceDispute(Strict):
    """The buyer tells the seller which lines of an unpaid invoice it does not accept."""

    lines: list[Annotated[str, Field(pattern=r"^L[1-9][0-9]?$")]] = Field(
        default_factory=list, max_length=10
    )
    text: str = Field(min_length=1, max_length=2_000)


class EventRequest(Strict):
    """A scheduled event the episode is delivering to commerce."""

    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PAGES",
    "CartLine",
    "CommerceConfig",
    "CommerceSettings",
    "DealMove",
    "DealSeed",
    "EventRequest",
    "InvoiceCreate",
    "InvoiceDispute",
    "InvoiceLine",
    "InvoiceWithdraw",
    "LoadRequest",
    "OfferRequest",
    "OfferSeed",
    "OrderKind",
    "OrderStatus",
    "PageContent",
    "PaymentLink",
    "PaymentStatusUpdate",
    "PlanSeed",
    "ProductSeed",
    "Rejection",
    "SubscriptionChange",
    "SubscriptionSeed",
    "SubscriptionStatus",
]
