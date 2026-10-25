"""The guard around any tool function: :func:`guarded`.

::

    @guarded(guard, world="payments", instruction=task_text, clock=sim_clock.now,
             fields=lambda args: {"amount": args["amount_paise"], "counterparty": args["payee"]})
    def pay(payee: str, amount_paise: int) -> dict: ...

Every call is decided first, with the function's arguments as the action's. ``allow``
runs it. ``block`` raises :class:`GuardBlocked`. ``ask_user`` calls ``confirm`` if one was
given (the user's answer; True runs the call and is recorded as the user's decision),
and otherwise raises :class:`GuardAskUser`. The function never runs unless the guard,
or the user, allowed it.

``clock`` gives the simulated time the request and its record carry; ``state`` and
``policy`` give the request's world state and delegated facts for the backend.
"""

from __future__ import annotations

import datetime as dt
import functools
import inspect
from collections.abc import Callable, Mapping
from typing import Any, ParamSpec, TypeVar

from guard.decision import ActionCall, DecisionRequest
from guard.engine import Guard, Outcome

P = ParamSpec("P")
R = TypeVar("R")
Args = Mapping[str, Any]


class GuardRefused(Exception):
    """The guard did not let a call run."""

    def __init__(self, outcome: Outcome) -> None:
        codes = ", ".join(outcome.reason_codes) or "no reason code"
        super().__init__(f"{outcome.tool}: {outcome.decision} ({codes})")
        self.outcome = outcome


class GuardBlocked(GuardRefused):
    """The guard blocked the call."""


class GuardAskUser(GuardRefused):
    """The call needs the user's confirmation, and nobody confirmed it."""


def request_from_call(
    *,
    world: str,
    instruction: str,
    tool: str,
    args: Args,
    now: dt.datetime,
    policy: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
) -> DecisionRequest:
    """The request for one call made outside a sandbox run (the decorator, the proxy)."""
    return DecisionRequest(
        world=world,
        instruction=instruction,
        policy=dict(policy or {}),
        state=dict(state or {}),
        action=ActionCall(tool=tool, args=dict(args)),
        sim_time=now,
    )


def guarded(
    guard: Guard,
    *,
    world: str,
    instruction: str,
    clock: Callable[[], dt.datetime],
    tool: str | None = None,
    fields: Callable[[Args], Mapping[str, Any]] | None = None,
    state: Callable[[Args], Mapping[str, Any]] | None = None,
    policy: Mapping[str, Any] | None = None,
    confirm: Callable[[Outcome], bool] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """A decorator that puts ``guard`` in front of a function."""

    def wrap(function: Callable[P, R]) -> Callable[P, R]:
        name = tool or function.__name__
        signature = inspect.signature(function)

        @functools.wraps(function)
        def call(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            request = request_from_call(
                world=world,
                instruction=instruction,
                tool=name,
                args=arguments,
                now=clock(),
                policy=policy,
                state=state(arguments) if state is not None else None,
            )
            outcome = guard.decide(request, fields(arguments) if fields is not None else None)
            if outcome.decision == "block":
                raise GuardBlocked(outcome)
            if outcome.decision == "ask_user":
                if confirm is None or not confirm(outcome):
                    raise GuardAskUser(outcome)
                guard.confirmed(outcome, request)
            return function(*args, **kwargs)

        return call

    return wrap


__all__ = ["GuardAskUser", "GuardBlocked", "GuardRefused", "guarded", "request_from_call"]
