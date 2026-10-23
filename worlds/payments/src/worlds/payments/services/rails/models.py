"""What a task may configure in the rails, and what callers may send them.

Money is integer paise everywhere. Every amount is a pydantic ``StrictInt``, so
``10.5``, ``10.0`` and ``"10"`` are refused instead of quietly becoming a
balance.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, model_validator

from kit.types import Strict

HANDLE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,47}@sandbox$"
"""Test-format handles only (``alice@sandbox``); no real UPI suffix can match."""

KEY_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
"""Idempotency keys: what a client would put in a header."""

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
"""Calendar days, weeks and months are counted in Indian Standard Time."""

Paise = Annotated[StrictInt, Field(ge=0)]
PositivePaise = Annotated[StrictInt, Field(gt=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
Handle = Annotated[str, Field(pattern=HANDLE_PATTERN)]
IdempotencyKey = Annotated[str, Field(pattern=KEY_PATTERN)]
MandateId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")]


class AccountStatus(StrEnum):
    """Whether an account can send or receive money."""

    ACTIVE = "active"
    FROZEN = "frozen"


class MandateType(StrEnum):
    """The three mandate shapes UPI publicly describes."""

    RECURRING = "recurring"
    ONE_TIME = "one_time"
    RESERVE = "reserve"


class MandateStatus(StrEnum):
    """``expired`` is never stored: it is worked out from ``end_at`` at read time."""

    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"


class Frequency(StrEnum):
    """How often a recurring mandate may be debited: once per calendar period, in IST."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class PaymentStatus(StrEnum):
    """``reversed`` means money left the payer while pending and was later returned."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REVERSED = "reversed"


class RefundStatus(StrEnum):
    """Refunds settle at once or fail; there is no pending refund in v0.1."""

    SUCCESS = "success"
    FAILED = "failed"


class DailyWindow(StrEnum):
    """How a "daily" cap is counted. Public sources disagree, so a task chooses."""

    ROLLING_24H = "rolling_24h"
    CALENDAR_DAY = "calendar_day"


class Decline(StrEnum):
    """Why a payment or refund was recorded as ``failed``. Stored on the row."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    PAYEE_NOT_FOUND = "payee_not_found"
    ACCOUNT_FROZEN = "account_frozen"
    LIMIT_PER_TXN_EXCEEDED = "limit_per_txn_exceeded"
    LIMIT_DAILY_EXCEEDED = "limit_daily_exceeded"
    LIMIT_MONTHLY_EXCEEDED = "limit_monthly_exceeded"
    LIMIT_MERCHANT_EXCEEDED = "limit_merchant_exceeded"
    MANDATE_NOT_FOUND = "mandate_not_found"
    MANDATE_MISMATCH = "mandate_mismatch"
    MANDATE_NOT_STARTED = "mandate_not_started"
    MANDATE_EXPIRED = "mandate_expired"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_PAUSED = "mandate_paused"
    MANDATE_AMOUNT_EXCEEDED = "mandate_amount_exceeded"
    MANDATE_LIMIT_EXHAUSTED = "mandate_limit_exhausted"
    MANDATE_FREQUENCY_EXCEEDED = "mandate_frequency_exceeded"
    REVERSED_AFTER_TIMEOUT = "reversed_after_timeout"
    REFUND_NOT_ALLOWED = "refund_not_allowed"
    REFUND_EXCEEDS_PAYMENT = "refund_exceeds_payment"


class Rejection(StrEnum):
    """Why a request was refused outright. Nothing is recorded, so it can be retried."""

    INVALID_REQUEST = "invalid_request"
    INVALID_AMOUNT = "invalid_amount"
    SAME_ACCOUNT = "same_account"
    PAYER_NOT_FOUND = "payer_not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ACCOUNT_NOT_FOUND = "account_not_found"
    MANDATE_NOT_FOUND = "mandate_not_found"
    PAYMENT_NOT_FOUND = "payment_not_found"
    REFUND_NOT_FOUND = "refund_not_found"
    INVALID_TRANSITION = "invalid_transition"
    ALREADY_LOADED = "already_loaded"
    UNKNOWN_EVENT = "unknown_event"
    UNKNOWN_RESOURCE = "unknown_resource"
    INVALID_MANDATE = "invalid_mandate"
    INSUFFICIENT_FUNDS = "insufficient_funds"


# ---------------------------------------------------------------- task config
class AccountSeed(Strict):
    """One account a task starts with."""

    handle: Handle
    holder: str = Field(min_length=1, max_length=80)
    balance_paise: Paise = 0
    kind: Literal["user", "merchant"] = "user"
    status: AccountStatus = AccountStatus.ACTIVE


class MandateSeed(Strict):
    """One mandate a task starts with. Times are offsets from the moment the task loads."""

    id: MandateId
    payer: Handle
    payee: Handle
    type: MandateType
    max_amount_paise: PositivePaise
    frequency: Frequency | None = None
    start_offset_seconds: StrictInt = 0
    end_offset_seconds: StrictInt | None = None
    remaining_limit_paise: Paise | None = None
    status: Literal["active", "paused", "revoked"] = "active"

    @model_validator(mode="after")
    def _consistent(self) -> MandateSeed:
        if self.payer == self.payee:
            msg = f"mandate {self.id!r}: payer and payee must differ"
            raise ValueError(msg)
        if (self.type is MandateType.RECURRING) != (self.frequency is not None):
            msg = f"mandate {self.id!r}: a frequency is required for recurring mandates only"
            raise ValueError(msg)
        if self.end_offset_seconds is not None and self.end_offset_seconds <= (
            self.start_offset_seconds
        ):
            msg = f"mandate {self.id!r}: end must be after start"
            raise ValueError(msg)
        if self.type is MandateType.RESERVE and self.end_offset_seconds is None:
            msg = f"mandate {self.id!r}: a reserve mandate needs an end (blocks expire)"
            raise ValueError(msg)
        bounded = self.type in (MandateType.ONE_TIME, MandateType.RESERVE)
        if (
            bounded
            and self.remaining_limit_paise is not None
            and self.remaining_limit_paise > self.max_amount_paise
        ):
            msg = f"mandate {self.id!r}: remaining limit cannot exceed the maximum amount"
            raise ValueError(msg)
        return self

    def initial_remaining(self) -> int | None:
        """What is left on the mandate at load time. ``None`` means no overall cap."""
        if self.remaining_limit_paise is not None:
            return self.remaining_limit_paise
        if self.type is MandateType.RECURRING:
            return None
        return self.max_amount_paise


class LimitsSeed(Strict):
    """A payer's caps. ``None`` means no cap of that kind."""

    per_txn_paise: PositivePaise | None = None
    daily_paise: PositivePaise | None = None
    monthly_paise: PositivePaise | None = None
    per_merchant_daily_paise: dict[Handle, PositivePaise] = Field(default_factory=dict)


class LimitChange(Strict):
    """A cap change at a scheduled time. Only the fields given are changed."""

    handle: Handle
    per_txn_paise: PositivePaise | None = None
    daily_paise: PositivePaise | None = None
    monthly_paise: PositivePaise | None = None
    per_merchant_daily_paise: dict[Handle, PositivePaise] | None = None


class ScheduledChange(Strict):
    """Something the world does on its own, ``after_seconds`` after the task loads."""

    after_seconds: PositiveInt
    name: Literal["rails.limit_change"]
    payload: LimitChange


class Faults(Strict):
    """Realistic trouble, switched on per task. Every draw is seeded."""

    timeout_on_attempts: list[PositiveInt] = Field(default_factory=list)
    """Payment attempts (1 = the first new idempotency key) that time out, always."""
    timeout_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    """Chance any other attempt times out, drawn from the task's seed."""
    timeout_outcome: Literal["success", "reversed"] = "success"
    """What a timed-out payment finally becomes."""
    settle_after_seconds: PositiveInt = 90
    """How long a timed-out payment stays pending. 90 s follows NPCI's first status check."""
    status_lag_seconds: Annotated[StrictInt, Field(ge=0)] = 0
    """How long status lookups keep saying ``pending`` after a payment has settled."""


class Settings(Strict):
    """Rules public sources leave open. See docs/DECISIONS.md."""

    daily_window: DailyWindow = DailyWindow.ROLLING_24H
    reserve_max_block_paise: PositivePaise = 1_000_000
    """Largest Reserve Pay block: ₹10,000, as reported for NPCI's circular."""
    reserve_max_validity_seconds: PositiveInt = 90 * 24 * 3600
    """Longest Reserve Pay block: 90 days, as reported for NPCI's circular."""


class RailsConfig(Strict):
    """The ``rails`` part of a payments task's ``initial_state``."""

    accounts: list[AccountSeed] = Field(min_length=1)
    mandates: list[MandateSeed] = Field(default_factory=list)
    limits: dict[Handle, LimitsSeed] = Field(default_factory=dict)
    faults: Faults = Field(default_factory=Faults)
    settings: Settings = Field(default_factory=Settings)
    scheduled: list[ScheduledChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> RailsConfig:
        handles = [account.handle for account in self.accounts]
        if len(set(handles)) != len(handles):
            msg = "account handles must be unique"
            raise ValueError(msg)
        known = set(handles)
        mandate_ids = [mandate.id for mandate in self.mandates]
        if len(set(mandate_ids)) != len(mandate_ids):
            msg = "mandate ids must be unique"
            raise ValueError(msg)
        referenced = [*self.limits, *(change.payload.handle for change in self.scheduled)]
        for mandate in self.mandates:
            referenced += [mandate.payer, mandate.payee]
        unknown = sorted(set(referenced) - known)
        if unknown:
            msg = f"unknown handles referenced: {unknown}"
            raise ValueError(msg)
        self._check_reserve_blocks()
        return self

    def _check_reserve_blocks(self) -> None:
        balances = {account.handle: account.balance_paise for account in self.accounts}
        blocked: dict[str, int] = {}
        for mandate in self.mandates:
            if mandate.type is not MandateType.RESERVE:
                continue
            assert mandate.end_offset_seconds is not None  # enforced by MandateSeed
            if mandate.max_amount_paise > self.settings.reserve_max_block_paise:
                msg = f"mandate {mandate.id!r}: block is above reserve_max_block_paise"
                raise ValueError(msg)
            validity = mandate.end_offset_seconds - mandate.start_offset_seconds
            if validity > self.settings.reserve_max_validity_seconds:
                msg = f"mandate {mandate.id!r}: block is longer than reserve_max_validity_seconds"
                raise ValueError(msg)
            if mandate.status != "revoked" and mandate.end_offset_seconds > 0:
                blocked[mandate.payer] = blocked.get(mandate.payer, 0) + (
                    mandate.initial_remaining() or 0
                )
        for payer, amount in blocked.items():
            if amount > balances[payer]:
                msg = f"{payer} cannot fund reserve blocks of {amount} paise"
                raise ValueError(msg)


# ------------------------------------------------------------------- requests
class PaymentRequest(Strict):
    """Move money from one handle to another, optionally under a mandate."""

    idempotency_key: IdempotencyKey
    payer: Handle
    payee: Handle
    amount_paise: StrictInt
    """Checked by the rails, not here, so a zero or negative amount gets its own code."""
    mandate_id: str | None = Field(default=None, max_length=48)
    note: str = Field(default="", max_length=140)


class RefundRequest(Strict):
    """Return some or all of a successful payment to its payer."""

    idempotency_key: IdempotencyKey
    payment_id: str = Field(min_length=1, max_length=64)
    amount_paise: StrictInt
    reason: str = Field(min_length=1, max_length=140)


class MandateRequest(Strict):
    """A payer authorising a payee to debit them later, as a UPI app's mandate screen does."""

    payer: Handle
    payee: Handle
    type: MandateType
    max_amount_paise: StrictInt
    frequency: Frequency | None = None
    valid_seconds: PositiveInt | None = None
    """How long the mandate lasts from now. Required for reserve mandates."""


class EventRequest(Strict):
    """A scheduled event the episode is delivering to the rails."""

    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "HANDLE_PATTERN",
    "IST",
    "AccountSeed",
    "AccountStatus",
    "DailyWindow",
    "Decline",
    "EventRequest",
    "Faults",
    "Frequency",
    "LimitChange",
    "LimitsSeed",
    "MandateRequest",
    "MandateSeed",
    "MandateStatus",
    "MandateType",
    "PaymentRequest",
    "PaymentStatus",
    "RailsConfig",
    "RefundRequest",
    "RefundStatus",
    "Rejection",
    "ScheduledChange",
    "Settings",
]
