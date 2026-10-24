"""What a task may configure in disputes, and what callers may send it.

Default windows follow public NPCI and RBI turnaround-time material where it
exists; see docs/DECISIONS.md for each source and which are invented.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt

from kit.types import Strict

from ...merchants import DAY, MerchantSeed, Slug
from ..rails.models import Handle

Seconds = Annotated[StrictInt, Field(gt=0)]


class RefundRequestStatus(StrEnum):
    """``expired``: the merchant ignored the request until its deadline."""

    OPEN = "open"
    ACCEPTED = "accepted"
    PARTIALLY_ACCEPTED = "partially_accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    REFUND_FAILED = "refund_failed"


class DisputeReason(StrEnum):
    """The UPI dispute types this world models."""

    WRONG_ITEM = "wrong_item"
    NOT_RECEIVED = "not_received"
    DUPLICATE_CHARGE = "duplicate_charge"


class DisputeStatus(StrEnum):
    """A dispute is decided once, at its deadline."""

    OPEN = "open"
    WON = "won"
    LOST = "lost"


class EvidenceKind(StrEnum):
    """What the user can point to. Every kind is an object in the world, by id."""

    ORDER = "order"
    PAYMENT = "payment"
    MESSAGE = "message"
    REFUND_REQUEST = "refund_request"


class Rejection(StrEnum):
    """Why disputes refused a request. Nothing is recorded."""

    INVALID_REQUEST = "invalid_request"
    INVALID_AMOUNT = "invalid_amount"
    MERCHANT_NOT_FOUND = "merchant_not_found"
    NOT_REFUNDABLE = "not_refundable"
    REFUND_REQUEST_OPEN = "refund_request_open"
    REFUND_REQUEST_NOT_FOUND = "refund_request_not_found"
    DISPUTE_EXISTS = "dispute_exists"
    DISPUTE_NOT_FOUND = "dispute_not_found"
    DISPUTE_WINDOW_CLOSED = "dispute_window_closed"
    EVIDENCE_WINDOW_CLOSED = "evidence_window_closed"
    INVALID_EVIDENCE = "invalid_evidence"
    MESSAGE_NOT_FOUND = "message_not_found"
    ALREADY_LOADED = "already_loaded"
    UNKNOWN_EVENT = "unknown_event"
    UNKNOWN_RESOURCE = "unknown_resource"


# ---------------------------------------------------------------- task config
class InboxSeed(Strict):
    """A message in the user's inbox at the start, or arriving later."""

    sender: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=140)
    body: str = Field(min_length=1, max_length=4_000)
    merchant_id: Slug | None = None
    order_id: str | None = Field(default=None, max_length=64)
    after_seconds: Annotated[StrictInt, Field(ge=0)] = 0
    """0: already in the inbox when the task starts."""


class DisputeSettings(Strict):
    """Windows a task may change. Defaults and their sources are in docs/DECISIONS.md."""

    dispute_window_seconds: Seconds = 45 * DAY
    """How long after a payment a dispute may be raised (NPCI's reported 45-day window)."""
    dispute_response_seconds: Seconds = 15 * DAY
    """How long the merchant side has to answer a dispute (NPCI's P2M response window)."""
    refund_response_seconds: Seconds = 5 * DAY
    """How long a merchant has to answer a refund request (RBI's T+5 for P2M, borrowed)."""
    require_merchant_contact: bool = True
    """Wrong-item and not-received disputes need a refund request to the merchant first."""


class DisputesConfig(Strict):
    """The ``disputes`` part of a payments task's ``initial_state``."""

    settings: DisputeSettings = Field(default_factory=DisputeSettings)
    inbox: list[InboxSeed] = Field(default_factory=list)


# ------------------------------------------------------------------- requests
class LoadRequest(Strict):
    """What the episode loads: the config, the user, and the merchants commerce has."""

    user: Handle
    merchants: list[MerchantSeed]
    config: DisputesConfig


class RefundRequestCreate(Strict):
    """A refund request the tool has already checked against the order and the rails."""

    order_id: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=64)
    merchant_id: Slug
    amount_paise: StrictInt
    refundable_paise: StrictInt
    reason: str = Field(min_length=1, max_length=500)


class DisputeCreate(Strict):
    """A dispute the tool has already checked against the rails and commerce."""

    payment_id: str = Field(min_length=1, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    merchant_id: Slug
    reason: DisputeReason
    amount_paise: StrictInt
    refundable_paise: StrictInt
    paid_at: str
    description: str = Field(min_length=1, max_length=1_000)


class EvidenceCreate(Strict):
    """One piece of evidence; the tool has already checked the reference exists."""

    kind: EvidenceKind
    reference_id: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


class MessageCreate(Strict):
    """A message from the user to a merchant."""

    merchant_id: Slug
    body: str = Field(min_length=1, max_length=2_000)
    order_id: str | None = Field(default=None, max_length=64)


class Notice(Strict):
    """Something another service tells the user, delivered to the inbox by the episode."""

    merchant_id: Slug | None = None
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8_000)
    order_id: str | None = None


class NoticeBatch(Strict):
    """Several notices at once."""

    notices: list[Notice]


class RefundOutcome(Strict):
    """What the rails did with a refund disputes asked for."""

    kind: Literal["refund_request", "dispute"]
    id: str = Field(min_length=1, max_length=64)
    refund_id: str | None = None
    status: Literal["success", "failed"]
    amount_paise: StrictInt


class EventRequest(Strict):
    """A scheduled event the episode is delivering, with any facts the rule needs."""

    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "DisputeCreate",
    "DisputeReason",
    "DisputeSettings",
    "DisputeStatus",
    "DisputesConfig",
    "EventRequest",
    "EvidenceCreate",
    "EvidenceKind",
    "InboxSeed",
    "LoadRequest",
    "MessageCreate",
    "Notice",
    "NoticeBatch",
    "RefundOutcome",
    "RefundRequestCreate",
    "RefundRequestStatus",
    "Rejection",
]
