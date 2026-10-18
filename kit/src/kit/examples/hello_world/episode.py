"""The hello-world episode: three tools over one service."""

from __future__ import annotations

from typing import Any

from kit.clock import SimClock
from kit.episode import BaseEpisode, ToolContext, ToolResult, ToolRouter, ToolSpec
from kit.rng import SeededRNG
from kit.types import TaskSpec

from .service import CounterService


def _counter(context: ToolContext) -> CounterService:
    service = context.services["counter"]
    assert isinstance(service, CounterService)
    return service


def _get_count(args: dict[str, Any], context: ToolContext) -> ToolResult:
    service = _counter(context)
    return ToolResult(
        text=f"The counter is at {service.count}.",
        data={"count": service.count, "ceiling": service.ceiling},
    )


def _increment(args: dict[str, Any], context: ToolContext) -> ToolResult:
    service = _counter(context)
    by = int(args.get("by", 1))
    diff = service.increment(by)
    return ToolResult(
        text=f"Counter is now {service.count}.",
        data={"count": service.count},
        state_diff=diff,
    )


def _finish(args: dict[str, Any], context: ToolContext) -> ToolResult:
    service = _counter(context)
    note = str(args.get("note", ""))
    return ToolResult(
        text=f"Finished at {service.count}." + (f" Note: {note}" if note else ""),
        data={"count": service.count, "note": note},
        done=True,
    )


def build_router() -> ToolRouter:
    """The tools the hello-world agent may call."""
    return ToolRouter(
        [
            ToolSpec(
                name="get_count",
                description="Read the current value of the counter.",
                handler=_get_count,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolSpec(
                name="increment",
                description="Add a positive amount to the counter.",
                handler=_increment,
                input_schema={
                    "type": "object",
                    "properties": {"by": {"type": "integer", "minimum": 1}},
                    "required": ["by"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="finish",
                description="Declare the task done and stop.",
                handler=_finish,
                terminal=True,
                input_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        ]
    )


class HelloWorldEpisode(BaseEpisode):
    """One counter service, the three tools, and the task's starting numbers."""

    world = "hello_world"

    def __init__(self, task: TaskSpec, clock: SimClock, rng: SeededRNG) -> None:
        service = CounterService(clock)
        super().__init__([service], build_router(), clock, rng)
        self._counter_service = service

    def _setup(self, task: TaskSpec) -> None:
        self._counter_service.load(
            count=int(task.initial_state.get("start", 0)),
            ceiling=task.initial_state.get("ceiling"),
        )


def make_episode(task: TaskSpec, clock: SimClock, rng: SeededRNG) -> HelloWorldEpisode:
    """Episode factory for the world registry."""
    return HelloWorldEpisode(task, clock, rng)
