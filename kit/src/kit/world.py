"""Worlds: the contract a domain must satisfy, and the registry that finds them.

The kit never imports a world. A world ships a
:class:`WorldSpec` through the ``agent_sandbox.worlds`` entry-point group, and
CI fails if anything outside ``worlds/`` imports a world package.
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kit.clock import SimClock
from kit.failures import failure_types
from kit.rng import SeededRNG
from kit.trace import GraderTrace, Trace
from kit.types import (
    ID_RE,
    FailureTypeDef,
    GradeResult,
    Observation,
    StepResult,
    TaskSpec,
    ToolCall,
)

ENTRY_POINT_GROUP = "agent_sandbox.worlds"


@runtime_checkable
class WorldState(Protocol):
    """One simulated service, snapshot-able and readable by graders.

    ``query`` is the read-only admin surface: graders and the runner use it,
    the agent never does.
    """

    name: str

    def snapshot(self) -> bytes:
        """Exact bytes of this service's state."""
        ...

    def restore(self, blob: bytes) -> None:
        """Replace state with a previous snapshot."""
        ...

    def query(self, resource: str, /, **params: Any) -> Any:
        """Read state. Must never mutate anything."""
        ...


class ReadOnlyService:
    """A :class:`WorldState` with only ``query`` exposed, handed to graders."""

    __slots__ = ("_service",)

    def __init__(self, service: WorldState) -> None:
        self._service = service

    @property
    def name(self) -> str:
        """The service's name, e.g. ``rails``."""
        return self._service.name

    def query(self, resource: str, /, **params: Any) -> Any:
        """Read state from the underlying service."""
        return self._service.query(resource, **params)

    def __repr__(self) -> str:
        return f"ReadOnlyService({self.name!r})"


class StateView(Mapping[str, ReadOnlyService]):
    """The final state of a world, as a mapping of service name to a read-only view."""

    def __init__(self, services: Sequence[WorldState]) -> None:
        self._views = {service.name: ReadOnlyService(service) for service in services}

    def __getitem__(self, name: str) -> ReadOnlyService:
        try:
            return self._views[name]
        except KeyError:
            msg = f"no service {name!r}; this world has {sorted(self._views)}"
            raise KeyError(msg) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._views)

    def __len__(self) -> int:
        return len(self._views)

    def __repr__(self) -> str:
        return f"StateView({sorted(self._views)})"


@runtime_checkable
class Grader(Protocol):
    """Plain Python over the final state and the grader's view of the trace."""

    def __call__(self, state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
        """Return the verdict for one episode."""
        ...


@runtime_checkable
class Episode(Protocol):
    """One run of one task inside one world."""

    def reset(self, task: TaskSpec) -> Observation:
        """Load the task's initial state and return the first observation."""
        ...

    def step(self, action: ToolCall) -> StepResult:
        """Apply one tool call."""
        ...

    def close(self) -> None:
        """Release services and restore the baseline snapshot."""
        ...

    @property
    def trace(self) -> Trace:
        """The full record of this episode."""
        ...

    @property
    def state(self) -> StateView:
        """Read-only state for graders."""
        ...


EpisodeFactory = Callable[[TaskSpec, SimClock, SeededRNG], Episode]


class WorldSpec(BaseModel):
    """Everything the kit needs to know about a world, and nothing domain-specific."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    name: str
    version: str
    categories: list[str]
    tool_names: list[str]
    task_schema: dict[str, Any] = Field(default_factory=dict)
    failure_types: list[FailureTypeDef] = Field(default_factory=list)
    episode_factory: EpisodeFactory = Field(exclude=True)
    tasks_dir: Path | None = None
    description: str = ""

    @field_validator("name", "version")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not ID_RE.match(value):
            msg = f"{value!r} must match {ID_RE.pattern}"
            raise ValueError(msg)
        return value

    def model_post_init(self, _context: Any) -> None:
        """Register this world's failure types, so its graders can emit them.

        A grader must be able to report ``payments.overspend`` whether or not
        the world was reached through the registry (tests build a WorldSpec
        directly), so registration belongs here rather than in the registry.
        """
        failure_types.register_world(self.name, self.failure_types)

    def validate_task(self, task: TaskSpec) -> TaskSpec:
        """Check a task belongs to this world and its ``initial_state`` fits the schema."""
        if task.world != self.name:
            msg = f"task {task.id!r} is for world {task.world!r}, not {self.name!r}"
            raise ValueError(msg)
        if task.category not in self.categories:
            msg = f"unknown category {task.category!r}; {self.name} has {self.categories}"
            raise ValueError(msg)
        unknown = sorted(set(task.allowed_tools) - set(self.tool_names))
        if unknown:
            msg = f"task {task.id!r} allows tools this world does not have: {unknown}"
            raise ValueError(msg)
        if self.task_schema:
            import jsonschema

            jsonschema.validate(task.initial_state, self.task_schema)
        return task

    def task_source(self) -> Any:
        """Load this world's packaged public tasks.

        Lets the runner find a world's tasks without importing the world.
        """
        from kit.tasks import TaskSource, load_tasks

        if self.tasks_dir is None:
            msg = f"world {self.name!r} does not ship a tasks directory"
            raise ValueError(msg)
        return TaskSource(load_tasks(self.tasks_dir))

    def make_episode(self, task: TaskSpec, clock: SimClock, rng: SeededRNG) -> Episode:
        """Build an episode for one task after validating it."""
        self.validate_task(task)
        return self.episode_factory(task, clock, rng)


class WorldRegistry:
    """Finds worlds through entry points and keeps their failure types registered."""

    def __init__(self) -> None:
        self._specs: dict[str, WorldSpec] = {}
        self._discovered = False

    def register(self, spec: WorldSpec) -> WorldSpec:
        """Add a world directly. Used by tests and by entry-point discovery."""
        self._specs[spec.name] = spec
        failure_types.register_world(spec.name, spec.failure_types)
        return spec

    def discover(self, *, force: bool = False) -> list[str]:
        """Load every installed world. Returns the names found."""
        if self._discovered and not force:
            return sorted(self._specs)
        for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
            loaded = entry.load()
            spec = loaded() if callable(loaded) and not isinstance(loaded, WorldSpec) else loaded
            if not isinstance(spec, WorldSpec):
                msg = f"entry point {entry.name!r} must provide a WorldSpec, got {type(spec)!r}"
                raise TypeError(msg)
            if spec.name != entry.name:
                msg = f"entry point {entry.name!r} provides world {spec.name!r}; names must match"
                raise ValueError(msg)
            self.register(spec)
        self._discovered = True
        return sorted(self._specs)

    def get(self, name: str) -> WorldSpec:
        """Look a world up, discovering installed ones first."""
        self.discover()
        try:
            return self._specs[name]
        except KeyError:
            msg = f"unknown world {name!r}; installed worlds: {sorted(self._specs)}"
            raise KeyError(msg) from None

    def names(self) -> list[str]:
        """Every installed world name."""
        self.discover()
        return sorted(self._specs)

    def __contains__(self, name: object) -> bool:
        self.discover()
        return name in self._specs


worlds = WorldRegistry()
"""The registry the runner and the CLI use."""


def load_grader(dotted: str) -> Grader:
    """Import a grader from a ``module.path:callable`` string."""
    module_path, _, attribute = dotted.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        msg = f"cannot import grader module {module_path!r} from {dotted!r}: {exc}"
        raise ImportError(msg) from exc
    try:
        grader = getattr(module, attribute)
    except AttributeError:
        msg = f"module {module_path!r} has no grader {attribute!r}"
        raise AttributeError(msg) from None
    if not callable(grader):
        msg = f"grader {dotted!r} is not callable"
        raise TypeError(msg)
    return grader  # type: ignore[no-any-return]


__all__ = [
    "ENTRY_POINT_GROUP",
    "Episode",
    "EpisodeFactory",
    "Grader",
    "ReadOnlyService",
    "StateView",
    "WorldRegistry",
    "WorldSpec",
    "WorldState",
    "load_grader",
    "worlds",
]
