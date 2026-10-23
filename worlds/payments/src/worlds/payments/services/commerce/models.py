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

    @model_validator(mode="after")
    def _references(self) -> CommerceConfig:
        merchant_ids = [merchant.id for merchant in self.merchants]
        _unique("merchant ids", merchant_ids)
        _unique("merchant handles", [merchant.handle for merchant in self.merchants])
        item_ids = [product.id for product in self.products] + [plan.id for plan in self.plans]
        _unique("product and plan ids", item_ids)
        _unique("offer codes", [(offer.merchant_id, offer.code) for offer in self.offers])
        _unique("subscription ids", [sub.id for sub in self.subscriptions])
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
        return self


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


class EventRequest(Strict):
    """A scheduled event the episode is delivering to commerce."""

    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PAGES",
    "CartLine",
    "CommerceConfig",
    "CommerceSettings",
    "EventRequest",
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
