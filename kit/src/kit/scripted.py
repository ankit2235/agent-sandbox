"""Running a fixed list of tool calls against a world.

This is how golden and wrong runs are executed: no model, no network, just a
script. CI uses it for every task in every world, and the determinism tests
compare its output byte for byte.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.trace import Trace
from kit.types import GradeResult, Observation, StepResult, TaskSpec, ToolCall
from kit.world import Episode, WorldSpec, load_grader


@dataclass(slots=True)
class ScriptRun:
    """Everything one scripted episode produced."""

    task: TaskSpec
    trace: Trace
    first_observation: Observation
    results: list[StepResult] = field(default_factory=list)
    grade: GradeResult | None = None
    final_state: dict[str, bytes] = field(default_factory=dict)

    @property
    def steps(self) -> int:
        """How many tool calls were applied."""
        return len(self.results)

    @property
    def passed(self) -> bool:
        """Whether the grader passed the run."""
        return bool(self.grade and self.grade.passed)

    @property
    def score(self) -> float:
        """The grader's score, or 0.0 if the run was not graded."""
        return self.grade.score if self.grade else 0.0


def run_script(
    spec: WorldSpec,
    task: TaskSpec,
    calls: Sequence[ToolCall],
    *,
    seed: int | None = None,
    start: dt.datetime = EPOCH,
    grade: bool = True,
    episode: Episode | None = None,
) -> ScriptRun:
    """Apply ``calls`` to a fresh episode, then grade it.

    Stops early when the episode reports done, which is how a step cap or a
    terminal tool ends a run.
    """
    clock = SimClock(start)
    rng = SeededRNG(seed if seed is not None else task.seed)
    active: Any = episode if episode is not None else spec.make_episode(task, clock, rng)
    first = active.reset(task)
    run = ScriptRun(task=task, trace=active.trace, first_observation=first)
    for call in calls:
        result = active.step(call)
        run.results.append(result)
        if result.done:
            break
    if grade:
        grader = load_grader(task.grader)
        run.grade = grader(active.state, active.trace.grader_view(), task)
    run.final_state = _snapshot(active)
    active.close()
    return run


def _snapshot(episode: Any) -> dict[str, bytes]:
    snapshot = getattr(episode, "snapshot_all", None)
    if callable(snapshot):
        result: dict[str, bytes] = snapshot()
        return result
    return {}


__all__ = ["ScriptRun", "run_script"]
