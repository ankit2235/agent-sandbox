"""The shape of a payments task's ``initial_state``.

Each service checks its own part; this model checks what spans them: the user
and every merchant are accounts in the rails, and a seeded subscription is
billed through a recurring mandate the user really gave that merchant.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from kit.types import Strict

from .services.commerce.models import CommerceConfig
from .services.disputes.models import DisputesConfig
from .services.rails.models import Handle, MandateType, RailsConfig


class PaymentsState(Strict):
    """``initial_state`` for every payments task. The agent acts as ``user``."""

    user: Handle
    rails: RailsConfig
    commerce: CommerceConfig = Field(default_factory=CommerceConfig)
    disputes: DisputesConfig = Field(default_factory=DisputesConfig)

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
        return self


def task_schema() -> dict[str, Any]:
    """JSON Schema for ``initial_state``, as the kit validates task files with."""
    return PaymentsState.model_json_schema()


__all__ = ["PaymentsState", "task_schema"]
