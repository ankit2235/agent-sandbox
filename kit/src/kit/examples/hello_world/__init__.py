"""The hello-world world: the template a new world copies.

One service, three tools, one grader, two tasks. It exists to prove the kit's
interfaces end to end — including through the OpenEnv adapter — without any
domain in the way.
"""

from __future__ import annotations

from pathlib import Path

from kit.tasks import TaskSource, load_tasks
from kit.types import FailureTypeDef
from kit.world import WorldSpec

from .episode import HelloWorldEpisode, build_router, make_episode
from .graders import reaches_target

TASKS_DIR = Path(__file__).parent / "tasks"

TASK_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "start": {"type": "integer", "minimum": 0},
        "target": {"type": "integer", "minimum": 1},
        "ceiling": {"type": ["integer", "null"], "minimum": 1},
    },
    "required": ["target"],
    "additionalProperties": False,
}

FAILURE_TYPES = [
    FailureTypeDef(
        id="hello_world.overshoot",
        label="Overshot the target",
        description="The counter went past the number the user asked for.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="hello_world.wrong_count",
        label="Wrong final count",
        description="The counter did not end on the target.",
    ),
]


def world_spec() -> WorldSpec:
    """The WorldSpec the kit's registry loads through the entry point."""
    return WorldSpec(
        name="hello_world",
        version="v0.1",
        description="A counter: the minimal world, used to test the kit's interfaces.",
        categories=["basics"],
        tool_names=build_router().names,
        task_schema=TASK_SCHEMA,
        tasks_dir=TASKS_DIR,
        failure_types=FAILURE_TYPES,
        episode_factory=make_episode,
    )


def task_source() -> TaskSource:
    """The example's two public tasks."""
    return TaskSource(load_tasks(TASKS_DIR))


__all__ = [
    "FAILURE_TYPES",
    "TASKS_DIR",
    "TASK_SCHEMA",
    "HelloWorldEpisode",
    "make_episode",
    "reaches_target",
    "task_source",
    "world_spec",
]
