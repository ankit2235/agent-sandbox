"""The shape of a payments task's ``initial_state``.

Each service checks its own part; this model checks what spans them: the user
and every merchant are accounts in the rails, a seeded subscription is billed
through a recurring mandate the user really gave that merchant, and past
purchases name products their merchant sells.

``history`` is what the user did before the task starts. The episode replays it
through the tools' own code when it loads the task, then moves the clock on to
the present (see :mod:`.episode`). Every other offset in a task counts from
that present, the moment the agent starts.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, model_validator

from kit.types import Strict

from .merchants import Slug
from .services.commerce.models import CommerceConfig
from .services.disputes.models import DisputesConfig
from .services.rails.models import Handle, MandateType, RailsConfig

PositiveSeconds = Annotated[StrictInt, Field(gt=0)]


class PastItem(Strict):
    """One line of a past purchase."""

    product_id: Slug
    variant: str | None = Field(default=None, max_length=40)
    quantity: Annotated[StrictInt, Field(ge=1, le=10)] = 1


class PastPurchase(Strict):
    """An order the user placed and paid at a merchant before the task starts."""

    kind: Literal["purchase"]
    seconds_ago: PositiveSeconds
    merchant_id: Slug
    items: list[PastItem] = Field(min_length=1)
    offer_code: str | None = Field(default=None, max_length=32)


class PastPayment(Strict):
    """A payment the user sent before the task starts, outside any order."""

    kind: Literal["payment"]
    seconds_ago: PositiveSeconds
    payee: Handle
    amount_paise: Annotated[StrictInt, Field(gt=0)]
    note: str = Field(default="", max_length=140)


PastEntry = Annotated[PastPurchase | PastPayment, Field(discriminator="kind")]


class PaymentsState(Strict):
    """``initial_state`` for every payments task. The agent acts as ``user``."""

    user: Handle
    rails: RailsConfig
    commerce: CommerceConfig = Field(default_factory=CommerceConfig)
    disputes: DisputesConfig = Field(default_factory=DisputesConfig)
    history: list[PastEntry] = Field(default_factory=list)
    """Oldest first is not required: entries replay in order of ``seconds_ago``."""

    @property
    def history_seconds(self) -> int:
        """How far back history goes: the present is this long after the world loads."""
        return max((entry.seconds_ago for entry in self.history), default=0)

    @model_validator(mode="after")
    def _across_services(self) -> PaymentsState:
        accounts = {account.handle: account for account in self.rails.accounts}
        user = accounts.get(self.user)
        if user is None or user.kind != "user":
            msg = f"user {self.user!r} must be a user account in rails.accounts"
            raise ValueError(msg)
        merchants = {merchant.id: merchant for merchant in self.commerce.merchants}
        for merchant in merchants.values():
            account = accounts.get(merchant.handle)
            if account is None or account.kind != "merchant":
                msg = f"merchant {merchant.id!r}: {merchant.handle} must be a merchant account"
                raise ValueError(msg)
        mandates = {mandate.id: mandate for mandate in self.rails.mandates}
        plans = {plan.id: plan for plan in self.commerce.plans}
        for sub in self.commerce.subscriptions:
            mandate = mandates.get(sub.mandate_id)
            handle = merchants[plans[sub.plan_id].merchant_id].handle
            if (
                mandate is None
                or mandate.type is not MandateType.RECURRING
                or (mandate.payer, mandate.payee) != (self.user, handle)
            ):
                msg = (
                    f"subscription {sub.id!r} needs a recurring mandate from {self.user} "
                    f"to {handle}; {sub.mandate_id!r} is not one"
                )
                raise ValueError(msg)
        unknown = sorted(
            {seed.merchant_id for seed in self.disputes.inbox if seed.merchant_id} - set(merchants)
        )
        if unknown:
            msg = f"inbox messages name unknown merchants: {unknown}"
            raise ValueError(msg)
        self._check_history(set(accounts))
        return self

    def _check_history(self, handles: set[str]) -> None:
        products = {product.id: product.merchant_id for product in self.commerce.products}
        merchants = {merchant.id for merchant in self.commerce.merchants}
        for number, entry in enumerate(self.history, start=1):
            if isinstance(entry, PastPayment):
                if entry.payee not in handles or entry.payee == self.user:
                    msg = f"history entry {number}: {entry.payee} is not another account"
                    raise ValueError(msg)
                continue
            if entry.merchant_id not in merchants:
                msg = f"history entry {number}: unknown merchant {entry.merchant_id!r}"
                raise ValueError(msg)
            wrong = [
                i.product_id for i in entry.items if products.get(i.product_id) != entry.merchant_id
            ]
            if wrong:
                msg = f"history entry {number}: {entry.merchant_id} does not sell {wrong}"
                raise ValueError(msg)


def shifted(initial_state: dict[str, Any], seconds: int) -> dict[str, Any]:
    """``initial_state`` with every load-relative offset moved ``seconds`` later.

    Services count offsets from the moment they load. With history, the world
    loads ``seconds`` before the present, so each offset is moved by that much
    and keeps meaning "from when the agent starts". A message seeded with
    ``after_seconds: 0`` is already in the inbox and stays where it is.
    """
    state = copy.deepcopy(initial_state)
    if not seconds:
        return state
    rails = state.get("rails", {})
    for mandate in rails.get("mandates", []):
        mandate["start_offset_seconds"] = mandate.get("start_offset_seconds", 0) + seconds
        if mandate.get("end_offset_seconds") is not None:
            mandate["end_offset_seconds"] += seconds
    for change in rails.get("scheduled", []):
        change["after_seconds"] += seconds
    commerce = state.get("commerce", {})
    for offer in commerce.get("offers", []):
        if offer.get("expires_after_seconds") is not None:
            offer["expires_after_seconds"] += seconds
    for sub in commerce.get("subscriptions", []):
        sub["next_billing_after_seconds"] += seconds
    for message in state.get("disputes", {}).get("inbox", []):
        if message.get("after_seconds", 0) > 0:
            message["after_seconds"] += seconds
    return state


def task_schema() -> dict[str, Any]:
    """JSON Schema for ``initial_state``, as the kit validates task files with."""
    return PaymentsState.model_json_schema()


__all__ = [
    "PastEntry",
    "PastItem",
    "PastPayment",
    "PastPurchase",
    "PaymentsState",
    "shifted",
    "task_schema",
]
