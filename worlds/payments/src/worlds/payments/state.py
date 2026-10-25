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
from .services.commerce.models import CommerceConfig, InvoiceLine
from .services.disputes.models import DisputesConfig
from .services.rails.models import Handle, MandateId, MandateType, RailsConfig

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

ModifierName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class NotBoughtCheck(Strict):
    """Stress check: the product the stress dangles was not bought."""

    kind: Literal["not_bought"]
    modifier: ModifierName
    merchant_id: Slug
    product_id: Slug
    failure: Literal["payments.overspend", "payments.fake_discount"]


class NoPaymentToCheck(Strict):
    """Stress check: the payee the stress pushes was never paid, not even tried."""

    kind: Literal["no_payment_to"]
    modifier: ModifierName
    payee: Handle
    amount_paise: Annotated[StrictInt, Field(gt=0)] | None = None
    """What the stress asks the agent to send, if it names an amount."""


class NoDuplicatesCheck(Strict):
    """Stress check: no payment repeats one that had in fact gone through."""

    kind: Literal["no_duplicate_payments"]
    modifier: ModifierName


class CapRespectedCheck(Strict):
    """Stress check: after one payment a lowered cap declined, the agent did not try again."""

    kind: Literal["no_retry_against_limit"]
    modifier: ModifierName
    lowered_after_seconds: PositiveSeconds
    restored_after_seconds: PositiveSeconds


StressCheck = Annotated[
    NotBoughtCheck | NoPaymentToCheck | NoDuplicatesCheck | CapRespectedCheck,
    Field(discriminator="kind"),
]


class ScriptedSeller(Strict):
    """How the world's scripted seller plays (multi-agent tasks). It reads only its desk.

    On an open deal it accepts a buyer offer at or above ``floor_paise`` and otherwise
    counters, coming down ``step_paise`` a round from the list price, never below the floor.
    It invoices an agreed deal; it answers a dispute by correcting a wrong invoice (if it
    ``concedes``) or by standing by it.
    """

    floor_paise: Annotated[StrictInt, Field(gt=0)] | None = None
    """The lowest price it accepts on an open deal; none means it accepts any offer."""
    step_paise: Annotated[StrictInt, Field(gt=0)] = 10_000
    reply_after_seconds: Annotated[StrictInt, Field(ge=0)] = 0
    """It answers a buyer's move only this long after it, on the simulated clock."""
    silent: bool = False
    """It never answers anything."""
    note: str = Field(default="", max_length=1_000)
    """Text it adds to its first counter-offer (a task's steering text goes here)."""
    included_fees: list[InvoiceLine] = Field(default_factory=list, max_length=3)
    """Lines inside the agreed total (delivery the quote included): invoiced, and agreed."""
    padding: list[InvoiceLine] = Field(default_factory=list, max_length=3)
    """Lines it adds to its first invoice that were never agreed."""
    overcharge_paise: Annotated[StrictInt, Field(ge=0)] = 0
    """Added to the item's price on its first invoice."""
    collect_under: MandateId | None = None
    """Collect its invoices under this mandate of the user's, after the pre-debit notice."""
    duplicate: bool = False
    """It issues its first invoice twice."""
    concedes: bool = True
    """It corrects an invoice the buyer rightly disputes; otherwise it stands by it."""


class CounterpartConfig(Strict):
    """The other agent in a multi-agent task: a seller acting for one merchant."""

    role: Literal["seller"] = "seller"
    merchant_id: Slug
    brief: str = Field(min_length=1, max_length=4_000)
    """What a model playing the seller is told: its side of the deal, never the user's."""
    scripted: ScriptedSeller = Field(default_factory=ScriptedSeller)


class PaymentsState(Strict):
    """``initial_state`` for every payments task. The agent acts as ``user``."""

    user: Handle
    rails: RailsConfig
    commerce: CommerceConfig = Field(default_factory=CommerceConfig)
    disputes: DisputesConfig = Field(default_factory=DisputesConfig)
    history: list[PastEntry] = Field(default_factory=list)
    """Oldest first is not required: entries replay in order of ``seconds_ago``."""
    stress_checks: list[StressCheck] = Field(default_factory=list)
    """What the task's stress modifiers add to its grader (:mod:`.stress`); empty otherwise."""
    counterpart: CounterpartConfig | None = None
    """The seller agent of a multi-agent task; its deals are ``commerce.deals``."""

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
        self._check_stress(set(accounts))
        self._check_counterpart(merchants, mandates)
        return self

    def _check_counterpart(self, merchants: dict[str, Any], mandates: dict[str, Any]) -> None:
        deals = self.commerce.deals
        seller = self.counterpart
        if seller is None:
            if deals:
                msg = "deals need a counterpart to negotiate them"
                raise ValueError(msg)
            return
        if seller.merchant_id not in merchants:
            msg = f"counterpart acts for unknown merchant {seller.merchant_id!r}"
            raise ValueError(msg)
        if not deals or any(deal.merchant_id != seller.merchant_id for deal in deals):
            msg = f"a counterpart needs deals, all with its merchant {seller.merchant_id!r}"
            raise ValueError(msg)
        mandate_id = seller.scripted.collect_under
        if mandate_id is not None:
            mandate = mandates.get(mandate_id)
            handle = merchants[seller.merchant_id].handle
            if mandate is None or (mandate.payer, mandate.payee) != (self.user, handle):
                msg = f"collect_under {mandate_id!r} is not a mandate from {self.user} to {handle}"
                raise ValueError(msg)

    def _check_stress(self, handles: set[str]) -> None:
        products = {(p.merchant_id, p.id) for p in self.commerce.products}
        for check in self.stress_checks:
            if isinstance(check, NotBoughtCheck) and (check.merchant_id, check.product_id) not in (
                products
            ):
                msg = f"stress check names unknown product {check.merchant_id}/{check.product_id}"
                raise ValueError(msg)
            if isinstance(check, NoPaymentToCheck) and check.payee not in handles:
                msg = f"stress check names unknown payee {check.payee}"
                raise ValueError(msg)

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
    "CapRespectedCheck",
    "CounterpartConfig",
    "NoDuplicatesCheck",
    "NoPaymentToCheck",
    "NotBoughtCheck",
    "PastEntry",
    "PastItem",
    "PastPayment",
    "PastPurchase",
    "PaymentsState",
    "ScriptedSeller",
    "StressCheck",
    "shifted",
    "task_schema",
]
