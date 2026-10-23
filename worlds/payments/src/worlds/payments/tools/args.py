"""Typed arguments for every payments tool.

Each model's JSON Schema is the tool's MCP ``inputSchema``; the same model
validates the call, so the schema the agent reads and the check it meets can
never drift apart. Amounts are integer paise (decision 21).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator

from kit.types import Strict

from ..services.disputes.models import DisputeReason, EvidenceKind
from ..services.rails.models import HANDLE_PATTERN, KEY_PATTERN, Frequency, MandateType

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
"""Ids go into service paths, so no slash, dot or percent sign can reach one (decision 81)."""
PAGE_PATTERN = r"^[a-z][a-z-]*(/[A-Za-z0-9][A-Za-z0-9_-]*)?$"
"""A page name, or ``product/<id>``: an unknown name is ``page_not_found``, a dot never passes."""

Id = Annotated[str, Field(min_length=1, max_length=64, pattern=ID_PATTERN)]
MerchantId = Annotated[
    str,
    Field(
        min_length=1, max_length=48, pattern=r"^[a-z0-9][a-z0-9_-]*$", description="A merchant id."
    ),
]
Paise = Annotated[StrictInt, Field(description="An amount in paise (₹1 = 100 paise).")]


class ToolArgs(Strict):
    """Base for tool arguments: unknown fields are refused."""


class NoArgs(ToolArgs):
    """The tool takes no arguments."""


class ListTransactions(ToolArgs):
    """Arguments for ``list_transactions``."""

    limit: Annotated[StrictInt, Field(ge=1, le=100)] = Field(
        default=20, description="How many entries, newest first."
    )
    status: Literal["pending", "success", "failed", "reversed"] | None = Field(
        default=None, description="Only entries with this status."
    )


class PaymentStatus(ToolArgs):
    """Arguments for ``get_payment_status``: exactly one of the two."""

    payment_id: Id | None = Field(default=None, description="The payment's id.")
    idempotency_key: Annotated[str, Field(pattern=KEY_PATTERN)] | None = Field(
        default=None, description="The key the payment was sent with."
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> PaymentStatus:
        if (self.payment_id is None) == (self.idempotency_key is None):
            msg = "give exactly one of payment_id and idempotency_key"
            raise ValueError(msg)
        return self


class SearchProducts(ToolArgs):
    """Arguments for ``search_products``. Every filter given must match."""

    query: Annotated[str, Field(max_length=100)] | None = Field(
        default=None, description="Words that must all appear in the name or description."
    )
    merchant_id: MerchantId | None = None
    category: Annotated[str, Field(max_length=40)] | None = None
    max_price_paise: Annotated[StrictInt, Field(ge=0)] | None = Field(
        default=None, description="Only items at or below this price, in paise."
    )


class ReadPage(ToolArgs):
    """Arguments for ``read_page``."""

    merchant_id: MerchantId
    page: Annotated[str, Field(min_length=1, max_length=80, pattern=PAGE_PATTERN)] = Field(
        description="home, terms, refund-policy, plans, account, or product/<product_id>."
    )


class MerchantOnly(ToolArgs):
    """Arguments naming one merchant."""

    merchant_id: MerchantId


class OrderOnly(ToolArgs):
    """Arguments naming one order."""

    order_id: Id


class ReadInbox(ToolArgs):
    """Arguments for ``read_inbox``."""

    unread_only: bool = Field(default=False, description="Only messages not read before.")


class AddToCart(ToolArgs):
    """Arguments for ``add_to_cart``."""

    merchant_id: MerchantId
    product_id: Id = Field(description="A product or plan id from search_products.")
    variant: Annotated[str, Field(max_length=40)] | None = Field(
        default=None, description="Required when the product has variants."
    )
    quantity: Annotated[StrictInt, Field(ge=0, le=10)] = Field(
        description="The line's new quantity. 0 removes the line."
    )


class ApplyOffer(ToolArgs):
    """Arguments for ``apply_offer``."""

    merchant_id: MerchantId
    code: Annotated[str, Field(min_length=1, max_length=32)]


class Pay(ToolArgs):
    """Arguments for ``pay``."""

    payee: Annotated[str, Field(pattern=HANDLE_PATTERN)] = Field(
        description="The handle to pay, e.g. shop@sandbox."
    )
    amount_paise: Paise
    idempotency_key: Annotated[str, Field(pattern=KEY_PATTERN)] = Field(
        description=(
            "A key you choose for this payment. Retrying with the same key never pays twice; "
            "a new key is a new payment."
        )
    )
    order_id: Id | None = Field(
        default=None, description="The order this pays, from checkout. Payee and amount must match."
    )
    mandate_id: Id | None = Field(
        default=None, description="Pay under this mandate. Required to start a subscription."
    )
    note: Annotated[str, Field(max_length=140)] = ""


class CreateMandate(ToolArgs):
    """Arguments for ``create_mandate``."""

    payee: Annotated[str, Field(pattern=HANDLE_PATTERN)]
    type: MandateType = Field(
        description=(
            "recurring: one debit per period up to max_amount_paise each; one_time: a single "
            "debit; reserve: blocks max_amount_paise now for several debits until it expires."
        )
    )
    max_amount_paise: Paise
    frequency: Frequency | None = Field(default=None, description="Required for recurring only.")
    valid_days: Annotated[StrictInt, Field(ge=1, le=3650)] | None = Field(
        default=None, description="How long the mandate lasts. Required for reserve."
    )


class MandateOnly(ToolArgs):
    """Arguments naming one mandate."""

    mandate_id: Id


class RequestRefund(ToolArgs):
    """Arguments for ``request_refund``."""

    order_id: Id
    amount_paise: Paise
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    payment_id: Id | None = Field(
        default=None,
        description="Which of the order's payments to refund. Default: the one that paid it.",
    )


class OpenDispute(ToolArgs):
    """Arguments for ``open_dispute``."""

    payment_id: Id
    reason: DisputeReason
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    amount_paise: Paise | None = Field(
        default=None, description="Default: everything not yet refunded."
    )


class SubmitEvidence(ToolArgs):
    """Arguments for ``submit_evidence``."""

    dispute_id: Id
    kind: EvidenceKind
    reference_id: Id = Field(description="The id of the order, payment, message or refund request.")
    note: Annotated[str, Field(max_length=500)] = ""


class SendMessage(ToolArgs):
    """Arguments for ``send_message``."""

    merchant_id: MerchantId
    body: Annotated[str, Field(min_length=1, max_length=2000)]
    order_id: Id | None = None


class SubscriptionAction(StrEnum):
    """What can be done to a subscription."""

    CHANGE_PLAN = "change_plan"
    CANCEL = "cancel"


class ChangeSubscription(ToolArgs):
    """Arguments for ``change_subscription``."""

    subscription_id: Id
    action: SubscriptionAction = Field(
        description="change_plan applies from the next renewal; cancel stops billing now."
    )
    plan_id: Id | None = Field(default=None, description="Required for change_plan.")


class Wait(ToolArgs):
    """Arguments for ``wait``."""

    hours: Annotated[float, Field(gt=0, le=720)] = Field(
        description="How long to wait, in hours (at most 30 days)."
    )


class AskUser(ToolArgs):
    """Arguments for ``ask_user``."""

    question: Annotated[str, Field(min_length=1, max_length=1000)] = Field(
        description="What the user needs to decide or confirm, and why."
    )


__all__ = [
    "AddToCart",
    "ApplyOffer",
    "AskUser",
    "ChangeSubscription",
    "CreateMandate",
    "ListTransactions",
    "MandateOnly",
    "MerchantOnly",
    "NoArgs",
    "OpenDispute",
    "OrderOnly",
    "Pay",
    "PaymentStatus",
    "ReadInbox",
    "ReadPage",
    "RequestRefund",
    "SearchProducts",
    "SendMessage",
    "SubmitEvidence",
    "SubscriptionAction",
    "ToolArgs",
    "Wait",
]
