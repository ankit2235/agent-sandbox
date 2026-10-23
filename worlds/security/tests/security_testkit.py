"""Shared helpers for the security world's tests."""

import copy
from typing import Any

import yaml

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.tasks import load_tasks
from kit.types import StepResult, TaskSpec, ToolCall
from worlds.security import world_spec
from worlds.security.episode import SecurityEpisode
from worlds.security.scenarios.expand import STORIES_DIR
from worlds.security.task_format import TASKS_DIR
from worlds.security.tools import TOOL_NAMES

TASKS = load_tasks(TASKS_DIR)
BY_ID = {task.id: task for task in TASKS}


def story_dict(story_id: str) -> dict[str, Any]:
    """A packaged story as plain data, to edit for a test."""
    raw: dict[str, Any] = yaml.safe_load((STORIES_DIR / f"{story_id}.yaml").read_text("utf-8"))
    return raw


def make_task(
    scenario: str | dict[str, Any] = "phish-workstation",
    *,
    seed: int = 7,
    max_steps: int = 60,
    grader: str = "worlds.security.graders.containment:phish_workstation",
    **state: Any,
) -> TaskSpec:
    """A task over a story, every tool allowed."""
    return TaskSpec(
        id="security-test",
        version="v0.1",
        world="security",
        category="containment",
        difficulty="easy",
        instruction="Test task.",
        initial_state={"scenario": copy.deepcopy(scenario), **state},
        allowed_tools=list(TOOL_NAMES),
        max_steps=max_steps,
        grader=grader,
        seed=seed,
    )


class Session:
    """One reset episode, with shortcuts for calling tools and reading the true state."""

    def __init__(self, task: TaskSpec, seed: int | None = None) -> None:
        self.task = task
        episode = world_spec().make_episode(
            task, SimClock(EPOCH), SeededRNG(task.seed if seed is None else seed)
        )
        assert isinstance(episode, SecurityEpisode)
        self.episode = episode
        self.first = episode.reset(task)

    def step(self, tool: str, **args: Any) -> StepResult:
        return self.episode.step(ToolCall(tool=tool, args=args))

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self.step(tool, **args)
        error = result.observation.error
        assert error is None, f"{tool} refused: {error}"
        return result.observation.data

    def refused(self, tool: str, **args: Any) -> str:
        error = self.step(tool, **args).observation.error
        assert error is not None, f"{tool} was accepted"
        return error.code

    def admin(self, service: str, resource: str = "state") -> Any:
        return self.episode.state[service].query(resource)
