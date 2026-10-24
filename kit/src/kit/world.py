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
from typing import Any, Literal, Protocol, runtime_checkable

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
    Strict,
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

WorldStatus = Literal["live", "preview", "coming_soon"]


class MetricDef(Strict):
    """One metric a leaderboard shows for a world. The runner computes it; the world labels it."""

    key: str
    label: str
    unit: str
    higher_is_better: bool
    primary: bool = False
    has_ci: bool = False


def default_metrics() -> list[MetricDef]:
    """What a world shows when it does not choose: the primary score, safety, cost, steps."""
    return [
        MetricDef(
            key="overall",
            label="Overall",
            unit="%",
            higher_is_better=True,
            primary=True,
            has_ci=True,
        ),
        MetricDef(key="safe_action_rate", label="Safe actions", unit="%", higher_is_better=True),
        MetricDef(key="avg_cost_usd", label="Cost / task", unit="$", higher_is_better=False),
        MetricDef(key="avg_steps", label="Avg steps", unit="steps", higher_is_better=False),
    ]


class Listing(Strict):
    """How a world presents itself on the leaderboard (``worlds.json``). Domain-neutral fields."""

    title: str
    status: WorldStatus = "preview"
    tagline: str = ""
    disclaimer: str | None = None
    category_labels: dict[str, str] = Field(default_factory=dict)
    """A label per category; a category without one is shown title-cased."""
    metrics: list[MetricDef] = Field(default_factory=default_metrics)
    unsafe_failures: list[str] | None = None
    """Failure types that make a run unsafe (``safe_action_rate``); default: high-impact ones."""


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
    runs_dir: Path | None = None
    """Where the world's golden and wrong run files live (:mod:`kit.scripted`), if it ships them."""
    listing: Listing | None = None
    """Leaderboard presentation; a world without one is not exported to ``worlds.json``."""

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
        if self.listing is not None:
            self._check_listing(self.listing)

    def _check_listing(self, listing: Listing) -> None:
        unknown = sorted(set(listing.category_labels) - set(self.categories))
        if unknown:
            msg = f"listing labels categories {self.name} does not have: {unknown}"
            raise ValueError(msg)
        known = {definition.id for definition in self.failure_types} | set(failure_types.ids("kit"))
        unknown = sorted(set(listing.unsafe_failures or []) - known)
        if unknown:
            msg = f"listing names failure types {self.name} cannot report: {unknown}"
            raise ValueError(msg)
        if sum(metric.primary for metric in listing.metrics) != 1:
            msg = f"listing for {self.name} must mark exactly one metric primary"
            raise ValueError(msg)

    def category_label(self, category: str) -> str:
        """The leaderboard label for one of this world's categories."""
        labels = self.listing.category_labels if self.listing else {}
        return labels.get(category, category.replace("_", " ").title())

    def unsafe_failures(self) -> set[str]:
        """Failure types that make a run unsafe: the listing's, else every high-impact one."""
        if self.listing is not None and self.listing.unsafe_failures is not None:
            return set(self.listing.unsafe_failures)
        own = {definition.id for definition in self.failure_types if definition.high_impact}
        return own | failure_types.high_impact_ids("kit")

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
    "Listing",
    "MetricDef",
    "ReadOnlyService",
    "StateView",
    "WorldRegistry",
    "WorldSpec",
    "WorldState",
    "WorldStatus",
    "default_metrics",
    "load_grader",
    "worlds",
]
