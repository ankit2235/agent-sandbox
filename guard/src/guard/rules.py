"""The hard rules: deterministic, always run first, and never overridden by a model.

:func:`evaluate` reads one attempted action's fields (:class:`Fields`, from the world's
``guard_fields`` or the caller), the rules that apply to its tool (:func:`guard.policy.
effective`), what this guard already allowed (:class:`Ledger`) and the world's checks, and
returns every rule the action breaks as a :class:`Hit`, in a fixed order. A rule that
needs a field the action does not have (a budget and no amount) is a hit too: ask_user,
never a silent allow.

Reason codes: ``guard.policy.*`` for the policy's own rules (listed in :data:`CODES`), and
each world check's own id.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from guard.decision import DecisionLabel
from guard.policy import DAYS, Effective, ListRule, PolicyError

Severity = Literal["block", "ask_user"]
ORDER: dict[DecisionLabel, int] = {"allow": 0, "ask_user": 1, "block": 2}

CODES = {
    "tool-not-delegated": "The tool is not one the task delegated to the agent.",
    "uncovered-tool": "The policy does not name this tool; its default applies.",
    "missing-amount": "A budget or threshold applies, but the action has no amount.",
    "missing-counterparty": "A counterparty rule applies, but the action names none.",
    "missing-category": "A category rule applies, but the action has none.",
    "over-per-action-budget": "The amount is above the per-action budget.",
    "over-daily-budget": "With this action, today's allowed amount goes over the daily budget.",
    "over-monthly-budget": "With this action, the month's allowed amount goes over its budget.",
    "confirm-above": "The amount is above the threshold the user confirms.",
    "counterparty-blocked": "The counterparty is blocked.",
    "counterparty-unlisted": "The counterparty is not on the allowed list.",
    "counterparty-confirm": "The user confirms actions with this counterparty.",
    "category-blocked": "The category is blocked.",
    "category-unlisted": "The category is not on the allowed list.",
    "category-confirm": "The user confirms actions in this category.",
    "mandate-required": "The action must name a mandate and names none.",
    "mandate-not-allowed": "The mandate named is not one the user allowed.",
    "outside-time-window": "The action falls outside the allowed time windows.",
}
"""The policy's own reason codes, each as ``guard.policy.<name>``."""


def code(name: str) -> str:
    """A policy reason code: ``guard.policy.<name>``."""
    if name not in CODES:
        msg = f"unknown policy reason {name!r}"
        raise KeyError(msg)
    return f"guard.policy.{name}"


class Fields(BaseModel):
    """What the policy's rules read from one action (strict: an amount is a whole number)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    amount: int | None = Field(default=None, ge=0)
    """In minor units (paise, cents), like the policy's budgets."""
    counterparty: str | None = None
    """Who or what the action is done to: a payee, a host, an account."""
    category: str | None = None
    mandate: str | None = None


class Check(Protocol):
    """A world's deterministic check, as :class:`guard.actions.Rule` is."""

    @property
    def decision(self) -> str:
        """``block`` or ``ask_user``."""
        ...

    @property
    def check(self) -> Callable[[dict[str, Any]], bool]:
        """True when the attempt breaks the check; given the request as JSON."""
        ...


@dataclass(frozen=True, slots=True)
class Hit:
    """One rule the action breaks."""

    code: str
    decision: DecisionLabel


@dataclass(slots=True)
class Ledger:
    """The amounts this guard allowed, per tool, with their simulated times."""

    entries: list[tuple[str, int, dt.datetime]] = field(default_factory=list)

    def add(self, tool: str, amount: int, when: dt.datetime) -> None:
        """Count an allowed (or user-confirmed) action."""
        self.entries.append((tool, amount, when))

    def spent(self, tool: str, when: dt.datetime, period: Literal["day", "month"]) -> int:
        """The total allowed for ``tool`` in the UTC day or month of ``when``."""
        at = when.astimezone(dt.UTC)
        total = 0
        for name, amount, moment in self.entries:
            then = moment.astimezone(dt.UTC)
            same = (then.year, then.month) == (at.year, at.month)
            if period == "day":
                same = same and then.day == at.day
            if name == tool and same:
                total += amount
        return total


def strictest(decisions: list[DecisionLabel]) -> DecisionLabel:
    """Block over ask_user over allow."""
    return max(decisions, key=lambda d: ORDER[d], default="allow")


def _list_rule(rule: ListRule, value: str | None, kind: str) -> list[Hit]:
    if value is None:
        return [Hit(code(f"missing-{kind}"), "ask_user")]
    if value in rule.block:
        return [Hit(code(f"{kind}-blocked"), "block")]
    hits: list[Hit] = []
    if rule.allow is not None and value not in rule.allow:
        hits.append(Hit(code(f"{kind}-unlisted"), rule.unlisted))
    if value in rule.confirm:
        hits.append(Hit(code(f"{kind}-confirm"), "ask_user"))
    return hits


def _in_window(rules: Effective, when: dt.datetime) -> list[Hit]:
    at = when.astimezone(dt.UTC)
    day, clock = DAYS[at.weekday()], at.strftime("%H:%M")
    hits: list[Hit] = []
    for rule in rules.rules:
        if rule.time_windows is None:
            continue
        inside = any(
            day in window.days and window.start <= clock < window.end
            for window in rule.time_windows.windows
        )
        if not inside:
            hits.append(Hit(code("outside-time-window"), rule.time_windows.outside))
    return hits


def evaluate(
    rules: Effective,
    *,
    tool: str,
    fields: Fields,
    request: dict[str, Any],
    when: dt.datetime,
    ledger: Ledger,
    checks: Mapping[str, Check],
    default: DecisionLabel,
) -> list[Hit]:
    """Every hard rule the action breaks, in a fixed order, each code once."""
    hits: list[Hit] = []
    allowed_tools = (request.get("policy") or {}).get("allowed_tools")
    if isinstance(allowed_tools, list) and tool not in allowed_tools:
        hits.append(Hit(code("tool-not-delegated"), "block"))
    if not rules.covered and default != "allow":
        hits.append(Hit(code("uncovered-tool"), default))
    amount = fields.amount
    for rule in rules.rules:
        needs_amount = rule.confirm_above is not None or (
            rule.budget is not None
            and any(
                v is not None
                for v in (rule.budget.per_action, rule.budget.daily, rule.budget.monthly)
            )
        )
        if needs_amount and amount is None:
            hits.append(Hit(code("missing-amount"), "ask_user"))
            continue
        if amount is None:
            continue
        budget = rule.budget
        if budget is not None:
            if budget.per_action is not None and amount > budget.per_action:
                hits.append(Hit(code("over-per-action-budget"), "block"))
            if budget.daily is not None and ledger.spent(tool, when, "day") + amount > budget.daily:
                hits.append(Hit(code("over-daily-budget"), "block"))
            if (
                budget.monthly is not None
                and ledger.spent(tool, when, "month") + amount > budget.monthly
            ):
                hits.append(Hit(code("over-monthly-budget"), "block"))
        if rule.confirm_above is not None and amount > rule.confirm_above:
            hits.append(Hit(code("confirm-above"), "ask_user"))
    for rule in rules.rules:
        if rule.counterparties is not None:
            hits += _list_rule(rule.counterparties, fields.counterparty, "counterparty")
        if rule.categories is not None:
            hits += _list_rule(rule.categories, fields.category, "category")
        if rule.mandate is not None:
            if rule.mandate.required and fields.mandate is None:
                hits.append(Hit(code("mandate-required"), "block"))
            elif (
                rule.mandate.allow is not None
                and fields.mandate is not None
                and fields.mandate not in rule.mandate.allow
            ):
                hits.append(Hit(code("mandate-not-allowed"), "block"))
    hits += _in_window(rules, when)
    for check_id in rules.checks:
        found = checks.get(check_id)
        if found is None:
            msg = f"the policy names check {check_id!r}, which no world provides"
            raise PolicyError(msg)
        if found.check(request):
            decision = found.decision
            if decision not in ("block", "ask_user"):
                msg = f"check {check_id!r} has decision {decision!r}"
                raise PolicyError(msg)
            hits.append(Hit(check_id, "block" if decision == "block" else "ask_user"))
    unique: dict[str, Hit] = {}
    for hit in hits:
        earlier = unique.get(hit.code)
        if earlier is None or ORDER[hit.decision] > ORDER[earlier.decision]:
            unique[hit.code] = Hit(hit.code, hit.decision)
    return [unique[name] for name in dict.fromkeys(hit.code for hit in hits)]


__all__ = [
    "CODES",
    "ORDER",
    "Check",
    "Fields",
    "Hit",
    "Ledger",
    "code",
    "evaluate",
    "strictest",
]
