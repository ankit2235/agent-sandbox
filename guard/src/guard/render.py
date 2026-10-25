"""One request as text: what every text-reading backend, our model and the near-duplicate check see.

The rendering is deterministic (sorted keys, fixed layout) so the same request is the
same text everywhere, and a long tool response is cut at ``max_chars`` with a marker
saying how much was cut.
"""

from __future__ import annotations

import json
from typing import Any

from guard.decision import DecisionRequest, StepView

MAX_CHARS = 1500
"""Longest rendering of one value (a response, the state, the policy) before it is cut."""


def compact(value: Any, max_chars: int = MAX_CHARS) -> str:
    """Canonical one-line JSON, cut to ``max_chars``."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}…[{len(text) - max_chars} more chars]"


def step_line(step: StepView, max_chars: int = MAX_CHARS) -> str:
    """One trace step as one line."""
    when = step.sim_time.strftime("%Y-%m-%d %H:%M")
    if step.kind == "event":
        return f"- [{when}] event {step.event}: {compact(step.args, max_chars)}"
    outcome = f"refused {step.error}" if step.error else compact(step.response, max_chars)
    return f"- [{when}] {step.tool} {compact(step.args, max_chars)} -> {outcome}"


def render(request: DecisionRequest, max_chars: int = MAX_CHARS) -> str:
    """The request as the text a model is shown."""
    lines = [
        f"World: {request.world}",
        f"Time: {request.sim_time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "User instruction:",
        request.instruction.strip(),
        "",
        "Delegated policy:",
        compact(request.policy, max_chars),
        "",
        "Relevant world state:",
        compact(request.state, max_chars),
        "",
        "Content the agent read earlier:",
        *([step_line(step, max_chars) for step in request.read] or ["- none"]),
        "",
        "Recent steps:",
        *([step_line(step, max_chars) for step in request.recent_steps] or ["- none"]),
        "",
        "Attempted action:",
        f"{request.action.tool} {compact(request.action.args, max_chars)}",
    ]
    return "\n".join(lines)


__all__ = ["MAX_CHARS", "compact", "render", "step_line"]
