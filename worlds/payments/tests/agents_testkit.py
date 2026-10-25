"""Helpers for the multi-agent (``agents``) tests: the buyer's calls, the scripted seller between.

:func:`drive` plays an episode the way the runner does (``runner.counterpart``): the scripted
seller acts before the buyer's first call and after each buyer call that reached the world.
"""

from dataclasses import dataclass, field
from typing import Any

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.scripted import Run, resolve_refs
from kit.tasks import load_tasks
from kit.types import GradeResult, StepResult, TaskSpec, ToolCall
from kit.world import load_grader
from worlds.payments import AGENT_TASKS_DIR, SELLER_ROLE, world_spec
from worlds.payments.episode import PaymentsEpisode

AGENT_TASKS = load_tasks(AGENT_TASKS_DIR)
SELLER_TOOLS = frozenset(SELLER_ROLE.tools)


def agent_task(task_id: str) -> TaskSpec:
    """One task of ``payments-v0.1-agents``."""
    return next(task for task in AGENT_TASKS if task.id == task_id)


@dataclass
class Driven:
    """An episode played by :func:`drive`, still open for reads, plus every call applied."""

    episode: PaymentsEpisode
    calls: list[ToolCall] = field(default_factory=list)
    results: list[StepResult] = field(default_factory=list)

    def grade(self) -> GradeResult:
        task = self.episode.task
        return load_grader(task.grader)(self.episode.state, self.episode.trace.grader_view(), task)

    def deal(self) -> dict[str, Any]:
        """The task's first deal, as the buyer's ``list_deals`` shows it."""
        deals: list[dict[str, Any]] = self.episode.commerce.call("GET", "/deals")
        return deals[0]


def _seller(driven: Driven) -> None:
    episode = driven.episode
    for role in episode.awaiting():
        for call in episode.scripted_turn(role):
            if episode.done:
                return
            driven.calls.append(call)
            driven.results.append(episode.step(call))


def drive(task: TaskSpec, buyer: list[ToolCall] | Run) -> Driven:
    """Play the buyer's calls (a run's non-seller steps, refs resolved) against the seller."""
    episode = world_spec().make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
    assert isinstance(episode, PaymentsEpisode)
    episode.reset(task)
    driven = Driven(episode)
    _seller(driven)
    steps = (
        [(call.tool, call.args, None) for call in buyer]
        if isinstance(buyer, list)
        else [(s.tool, s.args, s.save) for s in buyer.steps if s.tool not in SELLER_TOOLS]
    )
    saved: dict[str, Any] = {}
    for tool, args, save in steps:
        if episode.done:
            break
        call = ToolCall(tool=tool, args=resolve_refs(args, saved))
        result = episode.step(call)
        driven.calls.append(call)
        driven.results.append(result)
        if save is not None:
            observation = result.observation
            saved[save] = (
                None if observation.error else {"text": observation.text, **observation.data}
            )
        if not episode.done:
            _seller(driven)
    return driven


__all__ = ["AGENT_TASKS", "SELLER_TOOLS", "Driven", "agent_task", "drive"]
