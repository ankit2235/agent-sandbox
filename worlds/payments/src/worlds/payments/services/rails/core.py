"""The rails' rules, over one SQLite database per episode.

Plain Python and SQL: no web framework and no clock. Every operation is told
the simulated time, so ``app.py`` stays a thin HTTP layer and the property tests
can run thousands of cases quickly.

Money only ever moves through :meth:`Rails._move`, which writes two balanced
ledger entries. Three system accounts make the books add up:

- ``sys_issuance`` goes negative by exactly the opening balances, so the sum of
  every balance is always zero;
- ``sys_clearing`` holds money that is in flight (a payment that timed out and
  has not settled yet);
- ``sys_holds`` holds money blocked by Reserve Pay-style mandates.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Literal

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.service import SqliteStore
from kit.trace import DiffEntry

from ..common import Result, Row, SqlCore, iso, parse
from .models import (
    IST,
    AccountStatus,
    DailyWindow,
    Decline,
    Faults,
    Frequency,
    LimitChange,
    MandateRequest,
    MandateStatus,
    MandateType,
    PaymentRequest,
    PaymentStatus,
    RailsConfig,
    RefundRequest,
    RefundStatus,
    Rejection,
)

ISSUANCE = "sys_issuance"
CLEARING = "sys_clearing"
HOLDS = "sys_holds"
SYSTEM_ACCOUNTS = (
    (ISSUANCE, "Sandbox issuance (opening balances)"),
    (CLEARING, "In-flight payments"),
    (HOLDS, "Reserve Pay blocks"),
)

EVENT_SETTLE = "rails.settle"
EVENT_LIMIT_CHANGE = "rails.limit_change"
EVENT_RESERVE_EXPIRY = "rails.reserve_expiry"

DAY = dt.timedelta(days=1)
LIMIT_FIELDS = ("per_txn_paise", "daily_paise", "monthly_paise")
ADMIN_RESOURCES = ("state", "accounts", "mandates", "limits", "payments", "refunds", "ledger")
ADMIN_FILTERS = {
    "payments": ("id", "idempotency_key", "status", "payer", "payee", "mandate_id"),
    "refunds": ("payment_id", "status"),
}

SCHEMA = """
CREATE TABLE rails_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE account (
    id TEXT PRIMARY KEY,
    handle TEXT UNIQUE,
    holder TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'merchant', 'system')),
    status TEXT NOT NULL CHECK (status IN ('active', 'frozen')),
    balance_paise INTEGER NOT NULL CHECK (id = 'sys_issuance' OR balance_paise >= 0)
) STRICT;

CREATE TABLE limits (
    handle TEXT PRIMARY KEY REFERENCES account (handle),
    per_txn_paise INTEGER CHECK (per_txn_paise > 0),
    daily_paise INTEGER CHECK (daily_paise > 0),
    monthly_paise INTEGER CHECK (monthly_paise > 0)
) STRICT;

CREATE TABLE merchant_limit (
    payer TEXT NOT NULL REFERENCES account (handle),
    payee TEXT NOT NULL,
    daily_paise INTEGER NOT NULL CHECK (daily_paise > 0),
    PRIMARY KEY (payer, payee)
) STRICT;

CREATE TABLE mandate (
    id TEXT PRIMARY KEY,
    payer TEXT NOT NULL REFERENCES account (handle),
    payee TEXT NOT NULL REFERENCES account (handle),
    type TEXT NOT NULL CHECK (type IN ('recurring', 'one_time', 'reserve')),
    max_amount_paise INTEGER NOT NULL CHECK (max_amount_paise > 0),
    frequency TEXT CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    start_at TEXT NOT NULL,
    end_at TEXT,
    remaining_limit_paise INTEGER CHECK (remaining_limit_paise >= 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'revoked'))
) STRICT;

CREATE TABLE payment (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    attempt INTEGER NOT NULL,
    payer TEXT NOT NULL REFERENCES account (handle),
    payee TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    mandate_id TEXT,
    note TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed', 'reversed')),
    failure_code TEXT,
    failure_detail TEXT,
    funded_from TEXT REFERENCES account (id),
    timed_out INTEGER NOT NULL CHECK (timed_out IN (0, 1)),
    created_at TEXT NOT NULL,
    settles_at TEXT,
    settled_at TEXT,
    visible_at TEXT NOT NULL
) STRICT;

CREATE TABLE refund (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payment_id TEXT NOT NULL REFERENCES payment (id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    failure_code TEXT,
    failure_detail TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE ledger_entry (
    id INTEGER PRIMARY KEY,
    txn_ref TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES account (id),
    delta_paise INTEGER NOT NULL CHECK (delta_paise <> 0),
    created_at TEXT NOT NULL
) STRICT;
"""

DeclineInfo = tuple[Decline, dict[str, Any]]


# ---------------------------------------------------------------------- time
def day_start(now: dt.datetime) -> dt.datetime:
    """Midnight IST at the start of ``now``'s calendar day."""
    return now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)


def month_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The IST calendar month containing ``now``, as ``[start, end)``."""
    start = day_start(now).replace(day=1)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


def daily_window_start(now: dt.datetime, window: DailyWindow) -> dt.datetime:
    """The earliest moment a payment still counts towards a daily cap.

    A rolling window covers strictly the last 24 hours, so a payment made exactly
    24 hours ago has dropped out. Stored times are microsecond-precise, so
    "after ``now - 24h``" is "at or after ``now - 24h + 1 µs``".
    """
    if window is DailyWindow.ROLLING_24H:
        return now - DAY + dt.timedelta(microseconds=1)
    return day_start(now)


def period_bounds(now: dt.datetime, frequency: Frequency) -> tuple[dt.datetime, dt.datetime]:
    """The IST calendar period a recurring mandate may be debited once in."""
    if frequency is Frequency.DAILY:
        start = day_start(now)
        return start, start + DAY
    if frequency is Frequency.WEEKLY:
        start = day_start(now) - dt.timedelta(days=now.astimezone(IST).weekday())
        return start, start + 7 * DAY
    return month_bounds(now)


def account_id(handle: str) -> str:
    """The account id behind a handle: ``alice@sandbox`` -> ``acc_alice``."""
    return f"acc_{handle.split('@', 1)[0]}"


# --------------------------------------------------------------------- rails
class Rails(SqlCore):
    """Accounts, mandates, limits, payments, refunds and the ledger behind them."""

    service = "rails"

    def __init__(self, rng: SeededRNG, store: SqliteStore | None = None) -> None:
        super().__init__(rng, SCHEMA, store)
        self._config_cache: tuple[str, RailsConfig] | None = None

    # ------------------------------------------------------------- plumbing
    def _config(self) -> RailsConfig:
        row = self._one("SELECT value FROM rails_config WHERE key = 'config'")
        if row is None:
            msg = "the rails have not been loaded with a task yet"
            raise ToolRejected(Rejection.INVALID_REQUEST, msg)
        raw = str(row["value"])
        if self._config_cache is None or self._config_cache[0] != raw:
            self._config_cache = (raw, RailsConfig.model_validate_json(raw))
        return self._config_cache[1]

    def _account(self, handle: str) -> Row | None:
        return self._one("SELECT * FROM account WHERE handle = ?", (handle,))

    def _move(self, ref: str, source: str, target: str, amount: int, stamp: str) -> list[DiffEntry]:
        """Move money between two accounts with two balanced ledger entries."""
        diff: list[DiffEntry] = []
        for account, delta in ((source, -amount), (target, amount)):
            before = int(self._scalar("SELECT balance_paise FROM account WHERE id = ?", (account,)))
            self._db.execute(
                "UPDATE account SET balance_paise = ? WHERE id = ?", (before + delta, account)
            )
            self._db.execute(
                "INSERT INTO ledger_entry (txn_ref, account_id, delta_paise, created_at)"
                " VALUES (?, ?, ?, ?)",
                (ref, account, delta, stamp),
            )
            diff.append(
                DiffEntry(
                    op="update",
                    path=f"rails/account/{account}/balance_paise",
                    before=before,
                    after=before + delta,
                )
            )
        return diff

    # ----------------------------------------------------------------- load
    def load(self, config: RailsConfig, now: dt.datetime) -> Result:
        """Set up a task's starting state. A database is loaded once."""
        with self._transaction():
            if self._scalar("SELECT COUNT(*) FROM account"):
                msg = "the rails are already loaded; build a new service for a new task"
                raise ToolRejected(Rejection.ALREADY_LOADED, msg)
            stamp = iso(now)
            self._db.execute(
                "INSERT INTO rails_config (key, value) VALUES ('config', ?)",
                (config.model_dump_json(),),
            )
            for system_id, holder in SYSTEM_ACCOUNTS:
                self._db.execute(
                    "INSERT INTO account VALUES (?, NULL, ?, 'system', 'active', 0)",
                    (system_id, holder),
                )
            for seed in config.accounts:
                acc = account_id(seed.handle)
                self._db.execute(
                    "INSERT INTO account VALUES (?, ?, ?, ?, ?, 0)",
                    (acc, seed.handle, seed.holder, seed.kind, seed.status.value),
                )
                if seed.balance_paise:
                    self._move("opening", ISSUANCE, acc, seed.balance_paise, stamp)
            for handle, limits in config.limits.items():
                self._db.execute(
                    "INSERT INTO limits VALUES (?, ?, ?, ?)",
                    (handle, limits.per_txn_paise, limits.daily_paise, limits.monthly_paise),
                )
                for payee, cap in limits.per_merchant_daily_paise.items():
                    self._db.execute(
                        "INSERT INTO merchant_limit VALUES (?, ?, ?)", (handle, payee, cap)
                    )
            scheduled = self._load_mandates(config, now, stamp)
            for change in config.scheduled:
                scheduled.append(
                    ScheduledEvent(
                        at=now + dt.timedelta(seconds=change.after_seconds),
                        name=change.name,
                        payload=change.payload.model_dump(mode="json", exclude_unset=True),
                    )
                )
        counts = {"accounts": len(config.accounts), "mandates": len(config.mandates)}
        return Result(body={"loaded": counts}, scheduled=scheduled)

    def _load_mandates(
        self, config: RailsConfig, now: dt.datetime, stamp: str
    ) -> list[ScheduledEvent]:
        scheduled: list[ScheduledEvent] = []
        for seed in config.mandates:
            start = now + dt.timedelta(seconds=seed.start_offset_seconds)
            end = (
                None
                if seed.end_offset_seconds is None
                else now + dt.timedelta(seconds=seed.end_offset_seconds)
            )
            remaining = seed.initial_remaining()
            holds = (
                seed.type is MandateType.RESERVE
                and seed.status != "revoked"
                and end is not None
                and end > now
            )
            if seed.type is MandateType.RESERVE and not holds:
                remaining = 0
            self._db.execute(
                "INSERT INTO mandate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seed.id,
                    seed.payer,
                    seed.payee,
                    seed.type.value,
                    seed.max_amount_paise,
                    None if seed.frequency is None else seed.frequency.value,
                    iso(start),
                    None if end is None else iso(end),
                    remaining,
                    seed.status,
                ),
            )
            if holds and remaining:
                assert end is not None
                self._move(seed.id, account_id(seed.payer), HOLDS, remaining, stamp)
                scheduled.append(
                    ScheduledEvent(
                        at=end, name=EVENT_RESERVE_EXPIRY, payload={"mandate_id": seed.id}
                    )
                )
        return scheduled

    def arm_faults(self, faults: Faults) -> Result:
        """Switch faults on now, counting attempts from the next payment.

        A task with history replays its past payments with faults off, then arms
        them here, so ``timeout_on_attempts: [1]`` still means the agent's first
        payment and seeded timeout draws do not depend on how much history there is.
        """
        with self._transaction():
            config = self._config()
            updated = config.model_copy(update={"faults": faults})
            base = int(self._scalar("SELECT COUNT(*) FROM payment"))
            self._db.execute(
                "UPDATE rails_config SET value = ? WHERE key = 'config'",
                (updated.model_dump_json(),),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO rails_config (key, value) VALUES ('fault_base', ?)",
                (str(base),),
            )
        return Result(body={"faults": faults.model_dump(mode="json"), "fault_base": base})

    def _fault_base(self) -> int:
        row = self._one("SELECT value FROM rails_config WHERE key = 'fault_base'")
        return 0 if row is None else int(row["value"])

    # ------------------------------------------------------------- payments
    def create_payment(self, request: PaymentRequest, now: dt.datetime) -> Result:
        """Try a payment. Every accepted request is recorded, success or failure."""
        if request.amount_paise <= 0:
            msg = "amount_paise must be a positive whole number of paise"
            raise ToolRejected(Rejection.INVALID_AMOUNT, msg)
        if request.payer == request.payee:
            msg = "payer and payee are the same handle"
            raise ToolRejected(Rejection.SAME_ACCOUNT, msg)
        with self._transaction():
            config = self._config()
            existing = self._one(
                "SELECT * FROM payment WHERE idempotency_key = ?", (request.idempotency_key,)
            )
            if existing is not None:
                self._same_payment(existing, request)
                body = {"payment": self._payment_view(existing, now), "replayed": True}
                return Result(body={**body, "timed_out": False})
            payer = self._account(request.payer)
            if payer is None:
                msg = f"no account with handle {request.payer!r}"
                raise ToolRejected(Rejection.PAYER_NOT_FOUND, msg)
            mandate = (
                None
                if request.mandate_id is None
                else self._one("SELECT * FROM mandate WHERE id = ?", (request.mandate_id,))
            )
            attempt = int(self._scalar("SELECT COUNT(*) FROM payment")) + 1
            row: Row = {
                "id": f"pay_{self._rng.child(f'payment/{attempt}').token(12)}",
                "idempotency_key": request.idempotency_key,
                "attempt": attempt,
                "payer": request.payer,
                "payee": request.payee,
                "amount_paise": request.amount_paise,
                "mandate_id": request.mandate_id,
                "note": request.note,
                "status": PaymentStatus.FAILED.value,
                "failure_code": None,
                "failure_detail": None,
                "funded_from": None,
                "timed_out": 0,
                "created_at": iso(now),
                "settles_at": None,
                "settled_at": None,
                "visible_at": iso(now),
            }
            decline = self._check_payment(request, payer, mandate, config, now)
            if decline is not None:
                row["failure_code"] = decline[0].value
                row["failure_detail"] = json.dumps(decline[1], sort_keys=True)
                self._insert("payment", row)
                diff = [self._created("payment", row)]
                return Result(
                    body={
                        "payment": self._payment_view(row, now),
                        "replayed": False,
                        "timed_out": False,
                    },
                    state_diff=diff,
                    created=True,
                )
            return self._accept(row, payer, mandate, config, now)

    def _accept(
        self, row: Row, payer: Row, mandate: Row | None, config: RailsConfig, now: dt.datetime
    ) -> Result:
        faults = config.faults
        attempt = int(row["attempt"]) - self._fault_base()
        timed_out = attempt in faults.timeout_on_attempts or (
            faults.timeout_rate > 0
            and self._rng.child(f"timeout/{attempt}").chance(faults.timeout_rate)
        )
        reserve = mandate is not None and mandate["type"] == MandateType.RESERVE
        row["funded_from"] = HOLDS if reserve else payer["id"]
        lag = dt.timedelta(seconds=faults.status_lag_seconds)
        scheduled: list[ScheduledEvent] = []
        if timed_out:
            settles = now + dt.timedelta(seconds=faults.settle_after_seconds)
            row.update(
                status=PaymentStatus.PENDING.value,
                timed_out=1,
                settles_at=iso(settles),
                visible_at=iso(settles + lag),
            )
            target = CLEARING
            scheduled.append(
                ScheduledEvent(at=settles, name=EVENT_SETTLE, payload={"payment_id": row["id"]})
            )
        else:
            row.update(
                status=PaymentStatus.SUCCESS.value,
                settled_at=row["created_at"],
                visible_at=iso(now + lag),
            )
            target = account_id(str(row["payee"]))
        self._insert("payment", row)
        diff = [self._created("payment", row)]
        diff += self._move(
            str(row["id"]),
            str(row["funded_from"]),
            target,
            int(row["amount_paise"]),
            str(row["created_at"]),
        )
        if mandate is not None and mandate["remaining_limit_paise"] is not None:
            diff.append(
                self._set_remaining(
                    mandate, int(mandate["remaining_limit_paise"]) - int(row["amount_paise"])
                )
            )
        return Result(
            body={
                "payment": self._payment_view(row, now, lagged=False),
                "replayed": False,
                "timed_out": timed_out,
            },
            state_diff=diff,
            scheduled=scheduled,
            created=True,
        )

    def _same_payment(self, existing: Row, request: PaymentRequest) -> None:
        sent = (
            request.payer,
            request.payee,
            request.amount_paise,
            request.mandate_id,
            request.note,
        )
        stored = (
            existing["payer"],
            existing["payee"],
            existing["amount_paise"],
            existing["mandate_id"],
            existing["note"],
        )
        if sent != stored:
            msg = (
                f"idempotency key {request.idempotency_key!r} was already used for "
                f"payment {existing['id']} with different details"
            )
            raise ToolRejected(Rejection.IDEMPOTENCY_CONFLICT, msg)

    def _check_payment(
        self,
        request: PaymentRequest,
        payer: Row,
        mandate: Row | None,
        config: RailsConfig,
        now: dt.datetime,
    ) -> DeclineInfo | None:
        """The first rule a payment breaks, in the order a real switch would check them."""
        payee = self._account(request.payee)
        if payee is None:
            return Decline.PAYEE_NOT_FOUND, {"payee": request.payee}
        frozen = [acc["handle"] for acc in (payer, payee) if acc["status"] == AccountStatus.FROZEN]
        if frozen:
            return Decline.ACCOUNT_FROZEN, {"frozen": frozen}
        if request.mandate_id is not None:
            broken = self._check_mandate(request, mandate, now)
            if broken is not None:
                return broken
        broken = self._check_limits(request, config, now)
        if broken is not None:
            return broken
        reserve = mandate is not None and mandate["type"] == MandateType.RESERVE
        if not reserve and int(payer["balance_paise"]) < request.amount_paise:
            return Decline.INSUFFICIENT_FUNDS, {
                "balance_paise": payer["balance_paise"],
                "amount_paise": request.amount_paise,
            }
        return None

    def _check_mandate(
        self, request: PaymentRequest, mandate: Row | None, now: dt.datetime
    ) -> DeclineInfo | None:
        if mandate is None:
            return Decline.MANDATE_NOT_FOUND, {"mandate_id": request.mandate_id}
        if (mandate["payer"], mandate["payee"]) != (request.payer, request.payee):
            return Decline.MANDATE_MISMATCH, {
                "mandate_payer": mandate["payer"],
                "mandate_payee": mandate["payee"],
            }
        status = self._mandate_status(mandate, now)
        if status is MandateStatus.REVOKED:
            return Decline.MANDATE_REVOKED, {}
        if status is MandateStatus.EXPIRED:
            return Decline.MANDATE_EXPIRED, {"end_at": mandate["end_at"]}
        if status is MandateStatus.PAUSED:
            return Decline.MANDATE_PAUSED, {}
        if now < parse(str(mandate["start_at"])):
            return Decline.MANDATE_NOT_STARTED, {"start_at": mandate["start_at"]}
        kind = MandateType(mandate["type"])
        amount = request.amount_paise
        if kind is not MandateType.RESERVE and amount > int(mandate["max_amount_paise"]):
            return Decline.MANDATE_AMOUNT_EXCEEDED, {
                "max_amount_paise": mandate["max_amount_paise"]
            }
        if kind is MandateType.ONE_TIME and self._mandate_debits(str(mandate["id"])):
            return Decline.MANDATE_LIMIT_EXHAUSTED, {"remaining_limit_paise": 0}
        if kind is MandateType.RECURRING:
            start, end = period_bounds(now, Frequency(mandate["frequency"]))
            if self._mandate_debits(str(mandate["id"]), since=start):
                return Decline.MANDATE_FREQUENCY_EXCEEDED, {
                    "frequency": mandate["frequency"],
                    "next_period_at": iso(end),
                }
        remaining = mandate["remaining_limit_paise"]
        if remaining is not None and amount > int(remaining):
            return Decline.MANDATE_LIMIT_EXHAUSTED, {"remaining_limit_paise": remaining}
        return None

    def _check_limits(
        self, request: PaymentRequest, config: RailsConfig, now: dt.datetime
    ) -> DeclineInfo | None:
        amount = request.amount_paise
        limits = self._one("SELECT * FROM limits WHERE handle = ?", (request.payer,))
        window = config.settings.daily_window
        if limits is not None:
            cap = limits["per_txn_paise"]
            if cap is not None and amount > cap:
                return Decline.LIMIT_PER_TXN_EXCEEDED, {
                    "limit": "per_txn",
                    "cap_paise": cap,
                    "used_paise": 0,
                    "remaining_paise": cap,
                    "resets_at": None,
                }
            cap = limits["daily_paise"]
            if cap is not None:
                broken = self._window_cap(request, "daily", int(cap), now, window)
                if broken is not None:
                    return Decline.LIMIT_DAILY_EXCEEDED, broken
            cap = limits["monthly_paise"]
            if cap is not None:
                start, end = month_bounds(now)
                used, _ = self._spent(request.payer, start)
                if used + amount > cap:
                    return Decline.LIMIT_MONTHLY_EXCEEDED, {
                        "limit": "monthly",
                        "cap_paise": cap,
                        "used_paise": used,
                        "remaining_paise": max(cap - used, 0),
                        "resets_at": iso(end),
                    }
        merchant = self._one(
            "SELECT daily_paise FROM merchant_limit WHERE payer = ? AND payee = ?",
            (request.payer, request.payee),
        )
        if merchant is not None:
            broken = self._window_cap(
                request,
                "per_merchant_daily",
                int(merchant["daily_paise"]),
                now,
                window,
                payee=request.payee,
            )
            if broken is not None:
                return Decline.LIMIT_MERCHANT_EXCEEDED, broken
        return None

    def _window_cap(
        self,
        request: PaymentRequest,
        name: str,
        cap: int,
        now: dt.datetime,
        window: DailyWindow,
        payee: str | None = None,
    ) -> dict[str, Any] | None:
        """Check a daily cap and, if it is broken, say when room comes back."""
        start = daily_window_start(now, window)
        used, oldest = self._spent(request.payer, start, payee)
        if used + request.amount_paise <= cap:
            return None
        if window is DailyWindow.CALENDAR_DAY:
            resets_at: str | None = iso(start + DAY)
        else:
            resets_at = None if oldest is None else iso(parse(oldest) + DAY)
        return {
            "limit": name,
            "cap_paise": cap,
            "used_paise": used,
            "remaining_paise": max(cap - used, 0),
            "resets_at": resets_at,
        }

    def _spent(
        self, payer: str, since: dt.datetime, payee: str | None = None
    ) -> tuple[int, str | None]:
        """Money a payer has committed since a moment: successful and pending payments."""
        sql = (
            "SELECT COALESCE(SUM(amount_paise), 0), MIN(created_at) FROM payment"
            " WHERE payer = ? AND status IN ('success', 'pending') AND created_at >= ?"
        )
        params: tuple[Any, ...] = (payer, iso(since))
        if payee is not None:
            sql += " AND payee = ?"
            params += (payee,)
        total, oldest = self._db.execute(sql, params).fetchone()
        return int(total), oldest

    def _mandate_debits(self, mandate_id: str, since: dt.datetime | None = None) -> int:
        sql = (
            "SELECT COUNT(*) FROM payment WHERE mandate_id = ? AND status IN ('success', 'pending')"
        )
        params: tuple[Any, ...] = (mandate_id,)
        if since is not None:
            sql += " AND created_at >= ?"
            params += (iso(since),)
        return int(self._scalar(sql, params))

    @staticmethod
    def _mandate_status(mandate: Row, now: dt.datetime) -> MandateStatus:
        """``revoked`` wins; otherwise a mandate past its end is ``expired``."""
        stored = MandateStatus(mandate["status"])
        if stored is MandateStatus.REVOKED:
            return stored
        if mandate["end_at"] is not None and now >= parse(str(mandate["end_at"])):
            return MandateStatus.EXPIRED
        return stored

    def _set_remaining(self, mandate: Row, remaining: int) -> DiffEntry:
        self._db.execute(
            "UPDATE mandate SET remaining_limit_paise = ? WHERE id = ?", (remaining, mandate["id"])
        )
        return DiffEntry(
            op="update",
            path=f"rails/mandate/{mandate['id']}/remaining_limit_paise",
            before=mandate["remaining_limit_paise"],
            after=remaining,
        )

    # -------------------------------------------------------------- refunds
    def create_refund(self, request: RefundRequest, now: dt.datetime) -> Result:
        """Give money back from a successful payment. Refunds never exceed what was paid."""
        if request.amount_paise <= 0:
            msg = "amount_paise must be a positive whole number of paise"
            raise ToolRejected(Rejection.INVALID_AMOUNT, msg)
        with self._transaction():
            self._config()
            existing = self._one(
                "SELECT * FROM refund WHERE idempotency_key = ?", (request.idempotency_key,)
            )
            if existing is not None:
                sent = (request.payment_id, request.amount_paise, request.reason)
                stored = (existing["payment_id"], existing["amount_paise"], existing["reason"])
                if sent != stored:
                    msg = (
                        f"idempotency key {request.idempotency_key!r} was already used for "
                        f"refund {existing['id']} with different details"
                    )
                    raise ToolRejected(Rejection.IDEMPOTENCY_CONFLICT, msg)
                return Result(body={"refund": self._refund_view(existing), "replayed": True})
            payment = self._one("SELECT * FROM payment WHERE id = ?", (request.payment_id,))
            if payment is None:
                msg = f"no payment {request.payment_id!r}"
                raise ToolRejected(Rejection.PAYMENT_NOT_FOUND, msg)
            count = int(self._scalar("SELECT COUNT(*) FROM refund")) + 1
            row: Row = {
                "id": f"rfd_{self._rng.child(f'refund/{count}').token(12)}",
                "idempotency_key": request.idempotency_key,
                "payment_id": request.payment_id,
                "amount_paise": request.amount_paise,
                "status": RefundStatus.SUCCESS.value,
                "failure_code": None,
                "failure_detail": None,
                "reason": request.reason,
                "created_at": iso(now),
            }
            decline = self._check_refund(request, payment)
            if decline is not None:
                row.update(
                    status=RefundStatus.FAILED.value,
                    failure_code=decline[0].value,
                    failure_detail=json.dumps(decline[1], sort_keys=True),
                )
            self._insert("refund", row)
            diff = [self._created("refund", row)]
            if decline is None:
                diff += self._move(
                    str(row["id"]),
                    account_id(str(payment["payee"])),
                    account_id(str(payment["payer"])),
                    request.amount_paise,
                    str(row["created_at"]),
                )
        return Result(
            body={"refund": self._refund_view(row), "replayed": False},
            state_diff=diff,
            created=True,
        )

    def _check_refund(self, request: RefundRequest, payment: Row) -> DeclineInfo | None:
        if payment["status"] != PaymentStatus.SUCCESS:
            return Decline.REFUND_NOT_ALLOWED, {"payment_status": payment["status"]}
        refunded = self._refunded(str(payment["id"]))
        refundable = int(payment["amount_paise"]) - refunded
        if request.amount_paise > refundable:
            return Decline.REFUND_EXCEEDS_PAYMENT, {"refundable_paise": refundable}
        payee = self._account(str(payment["payee"]))
        assert payee is not None  # a successful payment always has a real payee
        if int(payee["balance_paise"]) < request.amount_paise:
            return Decline.INSUFFICIENT_FUNDS, {
                "balance_paise": payee["balance_paise"],
                "amount_paise": request.amount_paise,
            }
        return None

    def _refunded(self, payment_id: str) -> int:
        return int(
            self._scalar(
                "SELECT COALESCE(SUM(amount_paise), 0) FROM refund"
                " WHERE payment_id = ? AND status = 'success'",
                (payment_id,),
            )
        )

    # ------------------------------------------------------------- mandates
    def mandate_action(
        self, mandate_id: str, action: Literal["pause", "resume", "revoke"], now: dt.datetime
    ) -> Result:
        """Pause, resume or revoke a mandate, as its payer can in a UPI app."""
        allowed = {
            "pause": ({MandateStatus.ACTIVE}, MandateStatus.PAUSED),
            "resume": ({MandateStatus.PAUSED}, MandateStatus.ACTIVE),
            "revoke": ({MandateStatus.ACTIVE, MandateStatus.PAUSED}, MandateStatus.REVOKED),
        }
        sources, target = allowed[action]
        with self._transaction():
            mandate = self._one("SELECT * FROM mandate WHERE id = ?", (mandate_id,))
            if mandate is None:
                msg = f"no mandate {mandate_id!r}"
                raise ToolRejected(Rejection.MANDATE_NOT_FOUND, msg)
            status = self._mandate_status(mandate, now)
            if status not in sources:
                msg = f"cannot {action} a mandate that is {status.value}"
                raise ToolRejected(Rejection.INVALID_TRANSITION, msg)
            self._db.execute(
                "UPDATE mandate SET status = ? WHERE id = ?", (target.value, mandate_id)
            )
            diff = [
                DiffEntry(
                    op="update",
                    path=f"rails/mandate/{mandate_id}/status",
                    before=mandate["status"],
                    after=target.value,
                )
            ]
            if target is MandateStatus.REVOKED and mandate["type"] == MandateType.RESERVE:
                diff += self._release_hold(mandate, iso(now))
            updated = self._one("SELECT * FROM mandate WHERE id = ?", (mandate_id,))
            assert updated is not None
        return Result(body={"mandate": self._mandate_view(updated, now)}, state_diff=diff)

    def create_mandate(self, request: MandateRequest, now: dt.datetime) -> Result:
        """Create a mandate that starts now. A reserve mandate blocks its amount at once.

        Nothing is recorded when a rule is broken: setting up a mandate is a request
        the payer's app refuses, not a payment that fails.
        """
        if request.max_amount_paise <= 0:
            msg = "max_amount_paise must be a positive whole number of paise"
            raise ToolRejected(Rejection.INVALID_AMOUNT, msg)
        if request.payer == request.payee:
            msg = "payer and payee are the same handle"
            raise ToolRejected(Rejection.SAME_ACCOUNT, msg)
        recurring = request.type is MandateType.RECURRING
        if recurring != (request.frequency is not None):
            msg = "a frequency is required for recurring mandates, and only for them"
            raise ToolRejected(Rejection.INVALID_MANDATE, msg)
        with self._transaction():
            config = self._config()
            payer = self._account(request.payer)
            if payer is None:
                msg = f"no account with handle {request.payer!r}"
                raise ToolRejected(Rejection.PAYER_NOT_FOUND, msg)
            if self._account(request.payee) is None:
                msg = f"no account with handle {request.payee!r}"
                raise ToolRejected(Rejection.ACCOUNT_NOT_FOUND, msg)
            reserve = request.type is MandateType.RESERVE
            if reserve:
                self._check_reserve_request(request, config, int(payer["balance_paise"]))
            count = int(self._scalar("SELECT COUNT(*) FROM mandate")) + 1
            end = (
                None
                if request.valid_seconds is None
                else now + dt.timedelta(seconds=request.valid_seconds)
            )
            row: Row = {
                "id": f"mdt_{self._rng.child(f'mandate/{count}').token(12)}",
                "payer": request.payer,
                "payee": request.payee,
                "type": request.type.value,
                "max_amount_paise": request.max_amount_paise,
                "frequency": None if request.frequency is None else request.frequency.value,
                "start_at": iso(now),
                "end_at": None if end is None else iso(end),
                "remaining_limit_paise": None if recurring else request.max_amount_paise,
                "status": MandateStatus.ACTIVE.value,
            }
            self._insert("mandate", row)
            diff = [self._created("mandate", row)]
            scheduled: list[ScheduledEvent] = []
            if reserve:
                assert end is not None  # checked in _check_reserve_request
                diff += self._move(
                    str(row["id"]), str(payer["id"]), HOLDS, request.max_amount_paise, iso(now)
                )
                scheduled.append(
                    ScheduledEvent(
                        at=end, name=EVENT_RESERVE_EXPIRY, payload={"mandate_id": row["id"]}
                    )
                )
        return Result(
            body={"mandate": self._mandate_view(row, now)},
            state_diff=diff,
            scheduled=scheduled,
            created=True,
        )

    @staticmethod
    def _check_reserve_request(request: MandateRequest, config: RailsConfig, balance: int) -> None:
        settings = config.settings
        if request.valid_seconds is None:
            msg = "a reserve mandate needs valid_seconds: blocks always expire"
            raise ToolRejected(Rejection.INVALID_MANDATE, msg)
        if request.max_amount_paise > settings.reserve_max_block_paise:
            msg = f"a reserve block may be at most {settings.reserve_max_block_paise} paise"
            raise ToolRejected(Rejection.INVALID_MANDATE, msg)
        if request.valid_seconds > settings.reserve_max_validity_seconds:
            msg = f"a reserve block may last at most {settings.reserve_max_validity_seconds} s"
            raise ToolRejected(Rejection.INVALID_MANDATE, msg)
        if balance < request.max_amount_paise:
            msg = f"the balance ({balance} paise) cannot fund a block of {request.max_amount_paise}"
            raise ToolRejected(Rejection.INSUFFICIENT_FUNDS, msg)

    def _release_hold(self, mandate: Row, stamp: str) -> list[DiffEntry]:
        """Return what is left of a reserve block to the payer."""
        remaining = int(mandate["remaining_limit_paise"] or 0)
        if remaining == 0:
            return []
        diff = self._move(
            str(mandate["id"]), HOLDS, account_id(str(mandate["payer"])), remaining, stamp
        )
        diff.append(self._set_remaining(mandate, 0))
        return diff

    # --------------------------------------------------------------- events
    def apply_event(self, name: str, payload: dict[str, Any], now: dt.datetime) -> Result:
        """Apply something the world does on its own at a scheduled time."""
        with self._transaction():
            self._config()
            if name == EVENT_SETTLE:
                diff = self._settle(str(payload.get("payment_id")), now)
            elif name == EVENT_LIMIT_CHANGE:
                diff = self._change_limits(LimitChange.model_validate(payload))
            elif name == EVENT_RESERVE_EXPIRY:
                mandate = self._one(
                    "SELECT * FROM mandate WHERE id = ?", (payload.get("mandate_id"),)
                )
                if mandate is None or mandate["type"] != MandateType.RESERVE:
                    msg = f"no reserve mandate {payload.get('mandate_id')!r}"
                    raise ToolRejected(Rejection.MANDATE_NOT_FOUND, msg)
                diff = self._release_hold(mandate, iso(now))
            else:
                msg = f"the rails do not handle event {name!r}"
                raise ToolRejected(Rejection.UNKNOWN_EVENT, msg)
        return Result(body={"event": name}, state_diff=diff)

    def _settle(self, payment_id: str, now: dt.datetime) -> list[DiffEntry]:
        """Finish a timed-out payment. Settling twice does nothing the second time."""
        payment = self._one("SELECT * FROM payment WHERE id = ?", (payment_id,))
        if payment is None:
            msg = f"no payment {payment_id!r}"
            raise ToolRejected(Rejection.PAYMENT_NOT_FOUND, msg)
        if payment["status"] != PaymentStatus.PENDING:
            return []
        stamp = iso(now)
        amount = int(payment["amount_paise"])
        mandate = (
            None
            if payment["mandate_id"] is None
            else self._one("SELECT * FROM mandate WHERE id = ?", (payment["mandate_id"],))
        )
        diff: list[DiffEntry] = []
        if self._config().faults.timeout_outcome == "success":
            status, failure = PaymentStatus.SUCCESS, None
            diff += self._move(
                payment_id, CLEARING, account_id(str(payment["payee"])), amount, stamp
            )
        else:
            status, failure = PaymentStatus.REVERSED, Decline.REVERSED_AFTER_TIMEOUT.value
            back_to = str(payment["funded_from"])
            restore = mandate is not None and mandate["remaining_limit_paise"] is not None
            if (
                back_to == HOLDS
                and mandate is not None
                and self._mandate_status(mandate, now)
                in (
                    MandateStatus.REVOKED,
                    MandateStatus.EXPIRED,
                )
            ):
                # The block is gone, so the money goes home instead of back into it.
                back_to, restore = account_id(str(payment["payer"])), False
            diff += self._move(payment_id, CLEARING, back_to, amount, stamp)
            if restore and mandate is not None:
                remaining = int(mandate["remaining_limit_paise"]) + amount
                diff.append(self._set_remaining(mandate, remaining))
        self._db.execute(
            "UPDATE payment SET status = ?, failure_code = ?, settled_at = ? WHERE id = ?",
            (status.value, failure, stamp, payment_id),
        )
        diff.insert(
            0,
            DiffEntry(
                op="update",
                path=f"rails/payment/{payment_id}/status",
                before=payment["status"],
                after=status.value,
            ),
        )
        return diff

    def _change_limits(self, change: LimitChange) -> list[DiffEntry]:
        if self._account(change.handle) is None:
            msg = f"no account with handle {change.handle!r}"
            raise ToolRejected(Rejection.ACCOUNT_NOT_FOUND, msg)
        before = self._limits_row(change.handle)
        self._db.execute("INSERT OR IGNORE INTO limits (handle) VALUES (?)", (change.handle,))
        given = change.model_fields_set
        for name in LIMIT_FIELDS:
            if name in given:
                # `name` comes from the fixed LIMIT_FIELDS tuple, never from input.
                self._db.execute(
                    f"UPDATE limits SET {name} = ? WHERE handle = ?",
                    (getattr(change, name), change.handle),
                )
        if "per_merchant_daily_paise" in given:
            self._db.execute("DELETE FROM merchant_limit WHERE payer = ?", (change.handle,))
            for payee, cap in (change.per_merchant_daily_paise or {}).items():
                self._db.execute(
                    "INSERT INTO merchant_limit VALUES (?, ?, ?)", (change.handle, payee, cap)
                )
        return [
            DiffEntry(
                op="update",
                path=f"rails/limits/{change.handle}",
                before=before,
                after=self._limits_row(change.handle),
            )
        ]

    # -------------------------------------------------------------- reading
    def account(self, handle: str) -> Row:
        """One account, as its holder would see it."""
        row = self._account(handle)
        if row is None:
            msg = f"no account with handle {handle!r}"
            raise ToolRejected(Rejection.ACCOUNT_NOT_FOUND, msg)
        return {key: row[key] for key in ("handle", "holder", "kind", "status", "balance_paise")}

    def transactions(
        self, handle: str, now: dt.datetime, *, limit: int = 20, status: str | None = None
    ) -> list[Row]:
        """The holder's statement, newest first: payments in and out, and refunds.

        Payment statuses are what a status check would say, so they lag like one.
        A declined payment shows on the payer's statement only.
        """
        self.account(handle)
        entries: list[Row] = []
        payments = self.store.query_all(
            "SELECT * FROM payment WHERE payer = ? OR payee = ? ORDER BY rowid", (handle, handle)
        )
        for row in payments:
            view = self._payment_view(row, now)
            outgoing = row["payer"] == handle
            if not outgoing and row["status"] == PaymentStatus.FAILED:
                continue  # a payee never hears of a payment that was declined
            entries.append(
                {
                    "kind": "payment",
                    "id": view["id"],
                    "direction": "debit" if outgoing else "credit",
                    "counterparty": row["payee"] if outgoing else row["payer"],
                    "amount_paise": view["amount_paise"],
                    "status": view["status"],
                    "failure_code": view["failure_code"],
                    "mandate_id": view["mandate_id"],
                    "note": view["note"],
                    "payment_id": None,
                    "created_at": view["created_at"],
                }
            )
        refunds = self.store.query_all(
            "SELECT refund.*, payment.payer, payment.payee FROM refund"
            " JOIN payment ON payment.id = refund.payment_id"
            " WHERE payment.payer = ? OR payment.payee = ? ORDER BY refund.rowid",
            (handle, handle),
        )
        for row in refunds:
            incoming = row["payer"] == handle
            entries.append(
                {
                    "kind": "refund",
                    "id": row["id"],
                    "direction": "credit" if incoming else "debit",
                    "counterparty": row["payee"] if incoming else row["payer"],
                    "amount_paise": row["amount_paise"],
                    "status": row["status"],
                    "failure_code": row["failure_code"],
                    "mandate_id": None,
                    "note": row["reason"],
                    "payment_id": row["payment_id"],
                    "created_at": row["created_at"],
                }
            )
        if status is not None:
            entries = [entry for entry in entries if entry["status"] == status]
        # Stable: equal timestamps keep insertion order, payments before refunds.
        entries.sort(key=lambda entry: str(entry["created_at"]), reverse=True)
        return entries[:limit]

    def mandates_of(self, handle: str, now: dt.datetime) -> list[Row]:
        """Every mandate the holder has granted, with ``expired`` worked out."""
        self.account(handle)
        rows = self.store.query_all("SELECT * FROM mandate WHERE payer = ? ORDER BY id", (handle,))
        return [self._mandate_view(row, now) for row in rows]

    def lookup_handle(self, handle: str) -> Row:
        """Check an address before paying it, as UPI apps do."""
        row = self._account(handle)
        return {
            "handle": handle,
            "exists": row is not None,
            "holder": None if row is None else row["holder"],
        }

    def limits(self, handle: str, now: dt.datetime) -> Row:
        """A payer's caps and how much of the daily and monthly ones are used."""
        self.account(handle)
        window = self._config().settings.daily_window
        start = daily_window_start(now, window)
        return {
            **self._limits_row(handle),
            "daily_window": window.value,
            "daily_used_paise": self._spent(handle, start)[0],
            "monthly_used_paise": self._spent(handle, month_bounds(now)[0])[0],
        }

    def _limits_row(self, handle: str) -> Row:
        row = self._one("SELECT * FROM limits WHERE handle = ?", (handle,)) or {}
        merchants = self.store.query_all(
            "SELECT payee, daily_paise FROM merchant_limit WHERE payer = ? ORDER BY payee",
            (handle,),
        )
        return {
            "handle": handle,
            **{name: row.get(name) for name in LIMIT_FIELDS},
            "per_merchant_daily_paise": {m["payee"]: m["daily_paise"] for m in merchants},
        }

    def mandate(self, mandate_id: str, now: dt.datetime) -> Row:
        """One mandate, with ``expired`` worked out from the current time."""
        row = self._one("SELECT * FROM mandate WHERE id = ?", (mandate_id,))
        if row is None:
            msg = f"no mandate {mandate_id!r}"
            raise ToolRejected(Rejection.MANDATE_NOT_FOUND, msg)
        return self._mandate_view(row, now)

    def _mandate_view(self, row: Row, now: dt.datetime) -> Row:
        return {**row, "status": self._mandate_status(row, now).value}

    def payment(self, payment_id: str, now: dt.datetime) -> Row:
        """A payment's status as a status check reports it, which may lag."""
        row = self._one("SELECT * FROM payment WHERE id = ?", (payment_id,))
        if row is None:
            msg = f"no payment {payment_id!r}"
            raise ToolRejected(Rejection.PAYMENT_NOT_FOUND, msg)
        return self._payment_view(row, now)

    def payment_by_key(self, idempotency_key: str, now: dt.datetime) -> Row:
        """Find a payment by the key it was sent with: the "did it go through?" check."""
        row = self._one("SELECT * FROM payment WHERE idempotency_key = ?", (idempotency_key,))
        if row is None:
            msg = f"no payment was received with idempotency key {idempotency_key!r}"
            raise ToolRejected(Rejection.PAYMENT_NOT_FOUND, msg)
        return self._payment_view(row, now)

    def _payment_view(self, row: Row, now: dt.datetime, *, lagged: bool = True) -> Row:
        status, failure = row["status"], row["failure_code"]
        if lagged and status != PaymentStatus.FAILED and now < parse(str(row["visible_at"])):
            status, failure = PaymentStatus.PENDING.value, None
        return {
            "id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "payer": row["payer"],
            "payee": row["payee"],
            "amount_paise": row["amount_paise"],
            "mandate_id": row["mandate_id"],
            "note": row["note"],
            "status": status,
            "failure_code": failure,
            "refunded_paise": self._refunded(str(row["id"])),
            "created_at": row["created_at"],
        }

    def refund(self, refund_id: str) -> Row:
        """One refund."""
        row = self._one("SELECT * FROM refund WHERE id = ?", (refund_id,))
        if row is None:
            msg = f"no refund {refund_id!r}"
            raise ToolRejected(Rejection.REFUND_NOT_FOUND, msg)
        return self._refund_view(row)

    @staticmethod
    def _refund_view(row: Row) -> Row:
        return {key: value for key, value in row.items() if key != "failure_detail"}

    # ---------------------------------------------------------------- admin
    def admin(self, resource: str, now: dt.datetime, filters: dict[str, str]) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; the rails have {list(ADMIN_RESOURCES)}"
            raise ToolRejected(Rejection.UNKNOWN_RESOURCE, msg)
        allowed = ADMIN_FILTERS.get(resource, ())
        unknown = sorted(set(filters) - set(allowed))
        if unknown:
            msg = f"{resource} cannot be filtered by {unknown}; allowed: {list(allowed)}"
            raise ToolRejected(Rejection.INVALID_REQUEST, msg)
        if resource == "state":
            return {name: self.admin(name, now, {}) for name in ADMIN_RESOURCES[1:]}
        if resource == "accounts":
            return self.store.query_all("SELECT * FROM account ORDER BY id")
        if resource == "mandates":
            return [
                {**row, "effective_status": self._mandate_status(row, now).value}
                for row in self.store.query_all("SELECT * FROM mandate ORDER BY id")
            ]
        if resource == "limits":
            handles = self.store.query_all(
                "SELECT handle FROM limits UNION SELECT payer FROM merchant_limit ORDER BY 1"
            )
            return [self._limits_row(str(row["handle"])) for row in handles]
        if resource == "ledger":
            return self.store.query_all("SELECT * FROM ledger_entry ORDER BY id")
        table = "payment" if resource == "payments" else "refund"
        where = " AND ".join(f"{name} = ?" for name in sorted(filters))
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "") + " ORDER BY rowid"
        rows = self.store.query_all(sql, tuple(filters[name] for name in sorted(filters)))
        return [self._admin_row(row) for row in rows]

    @staticmethod
    def _admin_row(row: Row) -> Row:
        detail = row.get("failure_detail")
        out = {**row, "failure_detail": None if detail is None else json.loads(detail)}
        if "timed_out" in out:
            out["timed_out"] = bool(out["timed_out"])
        return out

    # -------------------------------------------------------------- helpers
    def _created(self, table: str, row: Row) -> DiffEntry:
        return DiffEntry(op="create", path=f"rails/{table}/{row['id']}", after=self._admin_row(row))


__all__ = [
    "CLEARING",
    "EVENT_LIMIT_CHANGE",
    "EVENT_RESERVE_EXPIRY",
    "EVENT_SETTLE",
    "HOLDS",
    "ISSUANCE",
    "SCHEMA",
    "Rails",
    "Result",
    "account_id",
    "iso",
]
