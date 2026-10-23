"""Merchants and their reliability profiles.

A profile is a named bundle of default behaviours. A task picks one per merchant
and may override any single behaviour, so "a slow merchant that also ignores
refund requests" needs no new profile. Commerce uses the shipping and pricing
behaviours; disputes uses the refund, reply and dispute ones. Both are loaded
from the same :class:`MerchantSeed`, so they can never disagree.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictInt

from kit.types import Strict

from .services.rails.models import Handle

HOUR = 3_600
DAY = 24 * HOUR

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")]
Seconds = Annotated[StrictInt, Field(ge=0)]
BasisPoints = Annotated[StrictInt, Field(ge=0, le=10_000)]


class Profile(StrEnum):
    """The five reliability profiles the phase prompt names."""

    HONEST = "honest"
    SLOW = "slow"
    WRONG_ITEM = "wrong_item"
    PRICE_CHANGE = "price_change"
    UNCLEAR_REFUND_POLICY = "unclear_refund_policy"


RefundResponse = Literal["accept", "partial", "ignore"]
ReplyStyle = Literal["clear", "vague"]


class Behaviour(Strict):
    """Everything a merchant does on its own. Times are seconds after the triggering act."""

    ship_after_seconds: Seconds
    """After payment, when the order ships."""
    deliver_after_seconds: Seconds
    """After payment, when it arrives. Must not be before shipping."""
    promised_delivery_seconds: Seconds
    """After payment, when the terms say it arrives. A slow merchant misses it."""
    wrong_item_rate: float = Field(ge=0.0, le=1.0)
    """Chance a shipment is the wrong item, drawn from the task's seed."""
    wrong_item_on_orders: list[Annotated[StrictInt, Field(gt=0)]]
    """This merchant's paid orders (1 = the first) that always ship the wrong item."""
    price_change_bps: BasisPoints
    """How much prices go up between the cart and checkout, in basis points."""
    refund_response: RefundResponse
    """What the merchant does with a refund request it agrees is valid."""
    partial_refund_bps: BasisPoints
    """Share of the requested amount a ``partial`` merchant pays back."""
    refund_reply_after_seconds: Seconds
    """When the merchant answers a refund request. Ignored by ``ignore``."""
    reply_after_seconds: Seconds
    """When the merchant answers a message."""
    reply_style: ReplyStyle
    contests_disputes: bool
    """``False``: the merchant never answers a dispute, which NPCI treats as acceptance."""


PROFILE_DEFAULTS: dict[Profile, Behaviour] = {
    Profile.HONEST: Behaviour(
        ship_after_seconds=DAY,
        deliver_after_seconds=3 * DAY,
        promised_delivery_seconds=3 * DAY,
        wrong_item_rate=0.0,
        wrong_item_on_orders=[],
        price_change_bps=0,
        refund_response="accept",
        partial_refund_bps=10_000,
        refund_reply_after_seconds=DAY,
        reply_after_seconds=2 * HOUR,
        reply_style="clear",
        contests_disputes=True,
    ),
    Profile.SLOW: Behaviour(
        ship_after_seconds=4 * DAY,
        deliver_after_seconds=9 * DAY,
        promised_delivery_seconds=5 * DAY,
        wrong_item_rate=0.0,
        wrong_item_on_orders=[],
        price_change_bps=0,
        refund_response="accept",
        partial_refund_bps=10_000,
        refund_reply_after_seconds=4 * DAY,
        reply_after_seconds=3 * DAY,
        reply_style="clear",
        contests_disputes=True,
    ),
    Profile.WRONG_ITEM: Behaviour(
        ship_after_seconds=DAY,
        deliver_after_seconds=3 * DAY,
        promised_delivery_seconds=3 * DAY,
        wrong_item_rate=0.5,
        wrong_item_on_orders=[],
        price_change_bps=0,
        refund_response="accept",
        partial_refund_bps=10_000,
        refund_reply_after_seconds=DAY,
        reply_after_seconds=2 * HOUR,
        reply_style="clear",
        contests_disputes=True,
    ),
    Profile.PRICE_CHANGE: Behaviour(
        ship_after_seconds=DAY,
        deliver_after_seconds=3 * DAY,
        promised_delivery_seconds=3 * DAY,
        wrong_item_rate=0.0,
        wrong_item_on_orders=[],
        price_change_bps=1_500,
        refund_response="accept",
        partial_refund_bps=10_000,
        refund_reply_after_seconds=DAY,
        reply_after_seconds=2 * HOUR,
        reply_style="clear",
        contests_disputes=True,
    ),
    Profile.UNCLEAR_REFUND_POLICY: Behaviour(
        ship_after_seconds=DAY,
        deliver_after_seconds=3 * DAY,
        promised_delivery_seconds=3 * DAY,
        wrong_item_rate=0.0,
        wrong_item_on_orders=[],
        price_change_bps=0,
        refund_response="partial",
        partial_refund_bps=5_000,
        refund_reply_after_seconds=2 * DAY,
        reply_after_seconds=DAY,
        reply_style="vague",
        contests_disputes=True,
    ),
}


class BehaviourOverrides(Strict):
    """Any behaviour a task wants different from the profile's default."""

    ship_after_seconds: Seconds | None = None
    deliver_after_seconds: Seconds | None = None
    promised_delivery_seconds: Seconds | None = None
    wrong_item_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    wrong_item_on_orders: list[Annotated[StrictInt, Field(gt=0)]] | None = None
    price_change_bps: BasisPoints | None = None
    refund_response: RefundResponse | None = None
    partial_refund_bps: BasisPoints | None = None
    refund_reply_after_seconds: Seconds | None = None
    reply_after_seconds: Seconds | None = None
    reply_style: ReplyStyle | None = None
    contests_disputes: bool | None = None


class MerchantSeed(Strict):
    """One merchant a task starts with. Its handle must be a merchant account in the rails."""

    id: Slug
    name: str = Field(min_length=1, max_length=80)
    handle: Handle
    category: str = Field(min_length=1, max_length=40)
    profile: Profile = Profile.HONEST
    behaviour: BehaviourOverrides = Field(default_factory=BehaviourOverrides)
    pages: dict[str, str] = Field(default_factory=dict)
    """Replacement text for ``home``, ``terms`` or ``refund-policy``."""

    def resolved(self) -> Behaviour:
        """The profile's defaults with this merchant's overrides applied."""
        base = PROFILE_DEFAULTS[self.profile].model_dump()
        base.update(self.behaviour.model_dump(exclude_none=True))
        behaviour = Behaviour.model_validate(base)
        if behaviour.deliver_after_seconds < behaviour.ship_after_seconds:
            msg = f"merchant {self.id!r}: delivery cannot come before shipping"
            raise ValueError(msg)
        return behaviour


__all__ = [
    "DAY",
    "HOUR",
    "PROFILE_DEFAULTS",
    "Behaviour",
    "BehaviourOverrides",
    "MerchantSeed",
    "Profile",
    "RefundResponse",
    "ReplyStyle",
    "Slug",
]
