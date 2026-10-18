"""Scripted runs: one golden, two plausibly wrong.

Every task in every world needs this pair (Phase 3 makes it a CI rule). The
hello-world example sets the pattern: a golden run the grader must pass, and
wrong runs it must fail.
"""

from __future__ import annotations

from kit.types import TaskSpec, ToolCall


def golden_run(task: TaskSpec) -> list[ToolCall]:
    """Read the counter, close the gap, stop. Must pass."""
    start = int(task.initial_state.get("start", 0))
    target = int(task.initial_state["target"])
    calls = [ToolCall(tool="get_count")]
    gap = target - start
    if gap > 0:
        calls.append(ToolCall(tool="increment", args={"by": gap}))
    calls.append(ToolCall(tool="finish", args={"note": f"counter is at {target}"}))
    return calls


def wrong_run_overshoots(task: TaskSpec) -> list[ToolCall]:
    """Adds one too many, then stops anyway. Must fail with hello_world.overshoot."""
    start = int(task.initial_state.get("start", 0))
    target = int(task.initial_state["target"])
    return [
        ToolCall(tool="get_count"),
        ToolCall(tool="increment", args={"by": target - start + 1}),
        ToolCall(tool="finish", args={"note": "done"}),
    ]


def wrong_run_never_finishes(task: TaskSpec) -> list[ToolCall]:
    """Lands on the target but never declares it done. Must fail."""
    start = int(task.initial_state.get("start", 0))
    target = int(task.initial_state["target"])
    calls = [ToolCall(tool="get_count")]
    for _ in range(target - start):
        calls.append(ToolCall(tool="increment", args={"by": 1}))
    calls.append(ToolCall(tool="get_count"))
    return calls


RUNS = {
    "golden": golden_run,
    "wrong_overshoots": wrong_run_overshoots,
    "wrong_never_finishes": wrong_run_never_finishes,
}
"""Named runs, so the CLI and the tests use exactly the same scripts."""
