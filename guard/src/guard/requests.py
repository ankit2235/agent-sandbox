"""The request a backend is given for one attempted action, built from an episode's trace.

The same builder serves the guard in front of a live sandbox run and the training code's
labelled rows, so a backend sees one attempt the same way in both. Nothing here reads a
world: the world's own module supplies the policy facts and the state around the attempt
(:class:`guard.actions.WorldActions`).
"""

from __future__ import annotations

from typing import Any

from guard.actions import WorldActions
from guard.decision import ActionCall, DecisionRequest, StepView
from guard.render import compact
from kit.trace import Trace, TraceStep
from kit.types import TaskSpec, ToolCall

RECENT_STEPS = 8
MAX_RESPONSE_CHARS = 2000


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


__all__ = ["MAX_RESPONSE_CHARS", "RECENT_STEPS", "opaque_tokens", "request_for"]
