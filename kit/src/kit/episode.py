"""The episode loop every world shares.

:class:`BaseEpisode` owns the things that must behave identically in every
world: the step cap, the allowed-tools check, draining scheduled events,
advancing simulated time, writing one trace step per tool call, and snapshot
and restore around the run. A world subclasses it and fills in two hooks:
``_setup`` (load the task's initial state) and its tool handlers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kit.clock import ScheduledEvent, SimClock
from kit.errors import EpisodeFinished, ToolRejected
from kit.rng import SeededRNG
from kit.trace import DiffEntry, Trace
from kit.types import Observation, StepResult, TaskSpec, ToolCall, ToolError
from kit.world import StateView, WorldState


@dataclass(slots=True)
class ToolResult:
    """What a tool handler returns."""

    text: str
    data: dict[str, Any] = field(default_factory=dict)
    state_diff: list[DiffEntry] = field(default_factory=list)
    done: bool = False
    reward: float = 0.0


@dataclass(slots=True)
class ToolContext:
    """What a tool handler is given. Services here are read-write; graders never see it."""

    task: TaskSpec
    clock: SimClock
    rng: SeededRNG
    services: dict[str, Any]
    step_index: int


ToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass(slots=True)
class ToolSpec:
    """One tool the agent can call, as the MCP server will expose it."""

    name: str
    description: str
    handler: ToolHandler
    input_schema: dict[str, Any] = field(default_factory=dict)
    terminal: bool = False


class ToolRouter:
    """The set of tools a world offers, and the dispatch into them."""

    def __init__(self, tools: Sequence[ToolSpec] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolSpec) -> ToolSpec:
        """Add a tool. Duplicate names are a programming error."""
        if tool.name in self._tools:
            msg = f"tool {tool.name!r} is already registered"
            raise ValueError(msg)
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        *,
        input_schema: dict[str, Any] | None = None,
        terminal: bool = False,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator form of :meth:`register`."""

        def decorate(handler: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    handler=handler,
                    input_schema=input_schema or {},
                    terminal=terminal,
                )
            )
            return handler

        return decorate

    @property
    def names(self) -> list[str]:
        """Tool names in registration order."""
        return list(self._tools)

    def get(self, name: str) -> ToolSpec:
        """Look a tool up."""
        try:
            return self._tools[name]
        except KeyError:
            msg = f"unknown tool {name!r}"
            raise KeyError(msg) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools


class BaseEpisode:
    """Shared episode mechanics. Worlds subclass this; the kit never subclasses it per world."""

    world: str = "unknown"
    step_seconds: float = 1.0
    """Simulated seconds each tool call takes, so deadlines mean something."""

    def __init__(
        self,
        services: Sequence[WorldState],
        router: ToolRouter,
        clock: SimClock,
        rng: SeededRNG,
        *,
        world: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if world is not None:
            self.world = world
        self._services = list(services)
        self._router = router
        self._clock = clock
        self._rng = rng
        self._run_id = run_id or f"{self.world}-{rng.token(8)}"
        self._task: TaskSpec | None = None
        self._trace = Trace(run_id=self._run_id, task_id="", world=self.world)
        self._state = StateView(self._services)
        self._baseline: dict[str, bytes] = {}
        self._step_count = 0
        self._done = False
        self._unauthorised: list[str] = []

    # ------------------------------------------------------------------ hooks
    def _setup(self, task: TaskSpec) -> None:
        """Load the task's ``initial_state`` into the services. Worlds override this."""

    def _on_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        """Apply a scheduled event. Worlds override this; default does nothing."""
        return []

    # ------------------------------------------------------------- properties
    @property
    def task(self) -> TaskSpec:
        """The task being run."""
        if self._task is None:
            msg = "episode has not been reset yet"
            raise EpisodeFinished(msg)
        return self._task

    @property
    def trace(self) -> Trace:
        """The full record of this episode."""
        return self._trace

    @property
    def state(self) -> StateView:
        """Read-only state for graders."""
        return self._state

    @property
    def clock(self) -> SimClock:
        """The episode's clock."""
        return self._clock

    @property
    def step_count(self) -> int:
        """How many tool calls have been applied."""
        return self._step_count

    @property
    def done(self) -> bool:
        """Whether the episode has ended."""
        return self._done

    @property
    def services(self) -> dict[str, Any]:
        """Read-write services, for tool handlers only."""
        return {service.name: service for service in self._services}

    # ------------------------------------------------------------------ cycle
    def reset(self, task: TaskSpec) -> Observation:
        """Load a task, snapshot every service and return the first observation."""
        unknown = sorted(set(task.allowed_tools) - set(self._router.names))
        if unknown:
            msg = f"task {task.id!r} allows tools this world does not have: {unknown}"
            raise ValueError(msg)
        self._task = task
        self._step_count = 0
        self._done = False
        self._unauthorised = []
        self._trace = Trace(run_id=self._run_id, task_id=task.id, world=self.world)
        self._setup(task)
        self._baseline = {service.name: service.snapshot() for service in self._services}
        return Observation(text=task.instruction, sim_time=self._clock.now())

    def step(self, action: ToolCall) -> StepResult:
        """Apply one tool call: events first, then the tool, then the caps."""
        if self._done:
            msg = "episode is finished; call reset() to run another task"
            raise EpisodeFinished(msg)
        task = self.task
        self._step_count += 1
        self._clock.advance(self.step_seconds)
        self._drain_events()

        error: ToolError | None = None
        result = ToolResult(text="")
        if action.tool not in self._router:
            error = ToolError(code="unknown_tool", message=f"no tool named {action.tool!r}")
        elif task.allowed_tools and action.tool not in task.allowed_tools:
            self._unauthorised.append(action.tool)
            error = ToolError(
                code="tool_not_allowed",
                message=f"{action.tool!r} is not available for this task",
            )
        else:
            spec = self._router.get(action.tool)
            context = ToolContext(
                task=task,
                clock=self._clock,
                rng=self._rng.child(f"tool/{action.tool}/{self._step_count}"),
                services=self.services,
                step_index=self._step_count,
            )
            try:
                result = spec.handler(dict(action.args), context)
            except ToolRejected as rejected:
                error = ToolError(code=rejected.code, message=rejected.message)
            if spec.terminal and error is None:
                result.done = True

        self._trace.add_tool_call(
            sim_time=self._clock.now(),
            tool=action.tool,
            args=dict(action.args),
            response=None if error else {"text": result.text, **result.data},
            error=error,
            state_diff=result.state_diff,
        )

        info: dict[str, Any] = {"step": self._step_count, "max_steps": task.max_steps}
        if self._unauthorised:
            info["unauthorised_tools"] = list(self._unauthorised)
        cap_reached = self._step_count >= task.max_steps
        if cap_reached:
            info["cap_reached"] = "max_steps"
        self._done = bool(result.done or cap_reached)

        observation = Observation(
            text=error.message if error else result.text,
            data={} if error else result.data,
            error=error,
            sim_time=self._clock.now(),
            done=self._done,
        )
        return StepResult(observation=observation, reward=result.reward, done=self._done, info=info)

    def close(self) -> None:
        """Restore the baseline snapshot so the next run starts clean."""
        for service in self._services:
            blob = self._baseline.get(service.name)
            if blob is not None:
                service.restore(blob)
            closer = getattr(service, "close", None)
            if callable(closer):
                closer()
        self._done = True

    # ----------------------------------------------------------------- events
    def _drain_events(self) -> None:
        for event in self._clock.due():
            diff = self._on_event(event)
            self._trace.add_event(
                sim_time=self._clock.now(),
                event=event.name,
                payload=dict(event.payload),
                state_diff=diff,
            )

    def snapshot_all(self) -> dict[str, bytes]:
        """Snapshot every service. Used by determinism tests and ``sandbox verify``."""
        return {service.name: service.snapshot() for service in self._services}


__all__ = [
    "BaseEpisode",
    "ToolContext",
    "ToolHandler",
    "ToolResult",
    "ToolRouter",
    "ToolSpec",
]
