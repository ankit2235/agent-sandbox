"""Labels from the graders: play a scripted run step by step and grade around each attempt.

For every attempt at a high-impact tool, the task's own grader is run on the live
state just before the attempt and just after it. A check that was not failing with a
failure type before and fails with it after was broken by the attempt; if that failure
type is one the world says an action can cause, the attempt was a mistake.

- No such check: **allow**.
- Otherwise **ask_user** if the task's golden run asks the user and none of the failure
  types is one the world always blocks (``ALWAYS_BLOCK``); else **block**.

A golden run's attempts must all come out allow (the run passes its grader); one that
does not is dropped and counted, never relabelled. A run that does not end as its file
declares (golden passes, a wrong run fails with its ``fails_with``) gives no rows.

The step loop is :func:`kit.scripted.play`'s, written out with the kit's public pieces
because ``play`` closes the episode before the state around each step can be read
(DECISIONS: a ``play`` hook is proposed for a later kit version).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from guard.decision import ActionCall, DecisionLabel, DecisionRequest, StepView
from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.scripted import GOLDEN, Run, RunError, resolve_refs
from kit.trace import Trace, TraceStep
from kit.types import GradeResult, TaskSpec, ToolCall
from kit.world import WorldSpec, load_grader

from ..render import compact
from ..world import WorldActions
from .example import Label

RECENT_STEPS = 8
MAX_RESPONSE_CHARS = 2000


@dataclass(slots=True)
class Attempt:
    """One attempt at a high-impact tool, with the grades around it."""

    step: int
    request: DecisionRequest
    before: GradeResult
    after: GradeResult


@dataclass(slots=True)
class Walk:
    """A run played step by step."""

    attempts: list[Attempt] = field(default_factory=list)
    grade: GradeResult | None = None
    behaved: bool = False
    """The run went as scripted and ended as its file declares."""


def _hide(value: Any, tokens: dict[str, str]) -> Any:
    """``value`` with every string equal to an opaque value replaced by its token."""
    if isinstance(value, str):
        return tokens.get(value, value)
    if isinstance(value, dict):
        return {key: _hide(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_hide(item, tokens) for item in value]
    return value


def opaque_tokens(steps: list[TraceStep], call: ToolCall, names: tuple[str, ...]) -> dict[str, str]:
    """Each opaque argument's values, in order of first use, mapped to ``<name>-<n>``."""
    tokens: dict[str, str] = {}
    counts: dict[str, int] = {}
    for args in [*(step.args or {} for step in steps if step.kind == "tool_call"), call.args]:
        for name in names:
            value = args.get(name)
            if isinstance(value, str) and value not in tokens:
                counts[name] = counts.get(name, 0) + 1
                tokens[value] = f"{name.replace('_', '-')}-{counts[name]}"
    return tokens


def _view(step: TraceStep, max_chars: int, tokens: dict[str, str]) -> StepView:
    response = _hide(step.response, tokens)
    if len(compact(response, max_chars=10**9)) > max_chars:
        response = {"truncated": compact(response, max_chars)}
    return StepView(
        kind="event" if step.kind == "event" else "tool_call",
        sim_time=step.sim_time,
        tool=step.tool,
        event=step.event,
        args=_hide(step.args or {}, tokens),
        response=response,
        error=step.error.code if step.error else None,
    )


def request_for(
    task: TaskSpec,
    trace: Trace,
    call: ToolCall,
    state: dict[str, Any],
    actions: WorldActions,
    now: Any,
    *,
    recent: int = RECENT_STEPS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> DecisionRequest:
    """The request a backend gets for ``call``, from the episode as it stands before it.

    Values of the world's opaque arguments become neutral tokens everywhere in it.
    """
    steps = [step for step in trace.steps if step.kind in ("tool_call", "event")]
    tokens = opaque_tokens(steps, call, actions.opaque_args)
    cut = max(len(steps) - recent, 0)
    earlier = [s for s in steps[:cut] if s.kind == "tool_call" and not s.state_diff and not s.error]
    return DecisionRequest(
        world=task.world,
        instruction=task.instruction,
        policy={"allowed_tools": sorted(task.allowed_tools), **actions.policy(task)},
        state=_hide(state, tokens),
        recent_steps=[_view(step, max_chars, tokens) for step in steps[cut:]],
        read=[_view(step, max_chars, tokens) for step in earlier],
        action=ActionCall(tool=call.tool, args=_hide(call.args, tokens)),
        sim_time=now,
    )


def walk(
    spec: WorldSpec,
    task: TaskSpec,
    run: Run,
    actions: WorldActions,
    *,
    recent: int = RECENT_STEPS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> Walk:
    """Play ``run`` on a fresh episode, grading before and after every high-impact attempt."""
    clock = SimClock(EPOCH)
    episode = spec.make_episode(task, clock, SeededRNG(task.seed))
    grader = load_grader(task.grader)
    out = Walk()
    saved_at = {n: step.save for n, step in enumerate(run.steps) if step.save is not None}
    episode.reset(task)
    try:
        played = 0
        for number, step in enumerate(run.steps):
            calls = episode.trace.tool_calls()
            saved = {name: calls[n].response for n, name in saved_at.items() if n < len(calls)}
            try:
                call = ToolCall(tool=step.tool, args=resolve_refs(step.args, saved))
            except RunError:
                return out
            before = None
            request = None
            if call.tool in actions.high_impact:
                before = grader(episode.state, episode.trace.grader_view(), task)
                context = actions.context(task, episode.state, call.tool, call.args)
                request = request_for(
                    task,
                    episode.trace,
                    call,
                    context,
                    actions,
                    clock.now(),
                    recent=recent,
                    max_chars=max_chars,
                )
            result = episode.step(call)
            played += 1
            error = result.observation.error
            if (error.code if error else None) != step.expect_error:
                return out
            if before is not None and request is not None:
                after = grader(episode.state, episode.trace.grader_view(), task)
                out.attempts.append(Attempt(number, request, before, after))
            if result.done:
                break
        out.grade = grader(episode.state, episode.trace.grader_view(), task)
        declared = run.fails_with
        ended_as_declared = (
            out.grade.passed if declared is None else out.grade.failure_type == declared
        )
        out.behaved = played == len(run.steps) and ended_as_declared
        return out
    finally:
        episode.close()


def _slug(name: str) -> str:
    """A check's reason code: its name without the task's arguments, ``paid_exactly(x, 1)``
    becoming ``paid-exactly``, so the same check gives the same code in every task.
    """
    base = name.split("(", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:60] or "check"


def broken_checks(attempt: Attempt, actions: WorldActions) -> list[tuple[str, str]]:
    """``(check name, failure type)`` for each check the attempt broke, in grader order."""
    before = {check.name: check for check in attempt.before.checks}
    broken: list[tuple[str, str]] = []
    for check in attempt.after.checks:
        if check.passed or check.failure_type not in actions.caused_by_action:
            continue
        earlier = before.get(check.name)
        if (
            earlier is not None
            and not earlier.passed
            and earlier.failure_type == check.failure_type
        ):
            continue
        broken.append((check.name, check.failure_type))
    return broken


def label(attempt: Attempt, actions: WorldActions, *, golden_asks: bool) -> Label:
    """The correct decision for one attempt."""
    broken = broken_checks(attempt, actions)
    if not broken:
        return Label(decision="allow")
    failures = sorted({failure for _, failure in broken})
    decision: DecisionLabel = (
        "ask_user" if golden_asks and not set(failures) & actions.always_block else "block"
    )
    checks = list(dict.fromkeys(name for name, _ in broken))
    codes = [*failures, *dict.fromkeys(f"{actions.world}.check.{_slug(n)}" for n in checks)]
    return Label(decision=decision, reason_codes=codes, checks=checks)


def golden_asks(runs: dict[str, Run], actions: WorldActions) -> bool:
    """Whether the task's golden run asks the user: then asking is the right way to stop."""
    return any(step.tool == actions.ask_tool for step in runs[GOLDEN].steps)


__all__ = [
    "MAX_RESPONSE_CHARS",
    "RECENT_STEPS",
    "Attempt",
    "Walk",
    "broken_checks",
    "golden_asks",
    "label",
    "opaque_tokens",
    "request_for",
    "walk",
]
