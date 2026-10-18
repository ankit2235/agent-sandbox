"""OpenEnv adapter.

Targets **openenv 0.9.0** (``huggingface/OpenEnv``, tag v0.9.0). In that
version an environment subclasses
``openenv.core.env_server.interfaces.Environment``, ``step()`` returns an
observation that carries ``reward`` and ``done`` (not a tuple), and the
optional ``TaskProvider`` methods are discovered structurally — which is how a
world's task sets become selectable splits.

OpenEnv is experimental upstream and pulls a large dependency tree, so it is an
extra: ``pip install 'agent-sandbox[openenv]'``. The import happens inside
:func:`environment_class`, which is why the class is built by a factory rather
than declared at module level.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Any

from pydantic import Field

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.tasks import TaskSource
from kit.types import GradeResult, TaskSpec, ToolCall
from kit.world import WorldSpec, load_grader, worlds

OPENENV_VERSION = "0.9.0"
INSTALL_HINT = "pip install 'agent-sandbox[openenv]'"


def _import_openenv() -> tuple[Any, Any, Any, Any]:
    try:
        from openenv.core.env_server.interfaces import Environment
        from openenv.core.env_server.types import Action, Observation, State
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        msg = f"the OpenEnv adapter needs openenv=={OPENENV_VERSION}: {INSTALL_HINT}"
        raise ImportError(msg) from exc
    return Environment, Action, Observation, State


@lru_cache(maxsize=1)
def environment_class() -> type[Any]:
    """Build (once) the OpenEnv ``Environment`` subclass that wraps a kit episode."""
    base_environment, base_action, base_observation, base_state = _import_openenv()

    class ToolCallAction(base_action):  # type: ignore[misc, valid-type]
        """One tool call, the only action any sandbox world accepts."""

        tool: str
        args: dict[str, Any] = Field(default_factory=dict)

    class SandboxObservation(base_observation):  # type: ignore[misc, valid-type]
        """A kit observation in OpenEnv's shape; ``done`` and ``reward`` are inherited."""

        text: str = ""
        data: dict[str, Any] = Field(default_factory=dict)
        error_code: str | None = None
        sim_time: str = ""

    class SandboxState(base_state):  # type: ignore[misc, valid-type]
        """Episode bookkeeping OpenEnv clients can read."""

        world: str = ""
        task_id: str = ""
        sim_time: str = ""
        done: bool = False

    class SandboxEnvironment(base_environment):  # type: ignore[misc, valid-type]
        """A sandbox world served through OpenEnv.

        Rewards are computed inside the environment, as OpenEnv expects: the
        world's grader runs when the episode finishes and its score becomes the
        final step's reward.
        """

        SUPPORTS_CONCURRENT_SESSIONS = False

        def __init__(
            self,
            world: str | WorldSpec,
            tasks: TaskSource,
            *,
            start_time: dt.datetime = EPOCH,
        ) -> None:
            super().__init__()
            self._spec = world if isinstance(world, WorldSpec) else worlds.get(world)
            self._tasks = tasks
            self._start_time = start_time
            self._episode: Any | None = None
            self._task: TaskSpec | None = None
            self._grade: GradeResult | None = None
            self._episode_id: str | None = None
            self._steps = 0

        # -- OpenEnv Environment ------------------------------------------
        def reset(
            self,
            seed: int | None = None,
            episode_id: str | None = None,
            *,
            split: str | None = None,
            index: int | None = None,
            task_id: str | None = None,
            **kwargs: Any,
        ) -> SandboxObservation:
            """Start a new episode on one task."""
            task = self._resolve_task(split=split, index=index, task_id=task_id)
            self.close()
            clock = SimClock(self._start_time)
            rng = SeededRNG(seed if seed is not None else task.seed)
            self._task = task
            self._grade = None
            self._steps = 0
            self._episode_id = (
                episode_id or f"{task.run_key}#{seed if seed is not None else task.seed}"
            )
            self._episode = self._spec.make_episode(task, clock, rng)
            observation = self._episode.reset(task)
            return SandboxObservation(
                text=observation.text,
                data=observation.data,
                sim_time=observation.sim_time.isoformat(),
                done=False,
                reward=None,
                metadata={"task_id": task.id, "world": task.world, "version": task.version},
            )

        def step(
            self, action: ToolCallAction, timeout_s: float | None = None, **kwargs: Any
        ) -> SandboxObservation:
            """Apply one tool call; grade the episode when it ends."""
            episode = self._require_episode()
            result = episode.step(ToolCall(tool=action.tool, args=dict(action.args)))
            self._steps += 1
            metadata: dict[str, Any] = dict(result.info)
            reward: float | None = result.reward
            if result.done:
                grade = self.grade()
                reward = grade.score
                metadata["grade"] = grade.model_dump(mode="json")
            return SandboxObservation(
                text=result.observation.text,
                data=result.observation.data,
                error_code=result.observation.error.code if result.observation.error else None,
                sim_time=result.observation.sim_time.isoformat(),
                done=result.done,
                reward=reward,
                metadata=metadata,
            )

        @property
        def state(self) -> SandboxState:
            """Current episode state."""
            episode = self._episode
            task = self._task
            return SandboxState(
                episode_id=self._episode_id,
                step_count=self._steps,
                world=self._spec.name,
                task_id=task.id if task else "",
                sim_time=episode.clock.now().isoformat() if episode else "",
                done=bool(episode.done) if episode else False,
            )

        def close(self) -> None:
            """Close the current episode, if any."""
            if self._episode is not None:
                self._episode.close()
                self._episode = None

        # -- TaskProvider (structural) ------------------------------------
        def list_splits(self) -> list[str]:
            """Task-set versions, e.g. ``["payments-v0.1"]``."""
            return self._tasks.splits()

        def list_tasks(self, split: str) -> list[dict[str, Any]]:
            """Task metadata for one split, without the grader internals."""
            return [
                self._task_summary(task, index)
                for index, task in enumerate(self._tasks.split(split))
            ]

        def num_tasks(self, split: str) -> int:
            """How many tasks the split holds."""
            return len(self._tasks.split(split))

        def get_task(self, split: str, index: int) -> dict[str, Any]:
            """One task's metadata."""
            tasks = self._tasks.split(split)
            if not 0 <= index < len(tasks):
                msg = f"index {index} out of range for split {split!r} ({len(tasks)} tasks)"
                raise IndexError(msg)
            return self._task_summary(tasks[index], index)

        def get_task_range(
            self, split: str, start: int | None = None, stop: int | None = None
        ) -> list[dict[str, Any]]:
            """Task metadata for a slice of a split."""
            tasks = self._tasks.split(split)[start:stop]
            offset = start or 0
            return [self._task_summary(task, offset + i) for i, task in enumerate(tasks)]

        def get_metadata(self) -> Any:
            """Describe this world to OpenEnv clients."""
            from openenv.core.env_server.types import EnvironmentMetadata

            return EnvironmentMetadata(
                name=self._spec.name,
                description=self._spec.description or f"{self._spec.name} sandbox world",
                version=self._spec.version,
            )

        # -- sandbox extras ------------------------------------------------
        def grade(self) -> GradeResult:
            """Run the task's grader over the final state and the grader's trace view."""
            if self._grade is not None:
                return self._grade
            episode = self._require_episode()
            task = self._task
            if task is None:  # pragma: no cover - reset() always sets it
                msg = "no task loaded"
                raise RuntimeError(msg)
            grader = load_grader(task.grader)
            self._grade = grader(episode.state, episode.trace.grader_view(), task)
            return self._grade

        @property
        def episode(self) -> Any:
            """The underlying kit episode, for tests and ``sandbox verify``."""
            return self._require_episode()

        def _require_episode(self) -> Any:
            if self._episode is None:
                msg = "call reset() before step()"
                raise RuntimeError(msg)
            return self._episode

        def _resolve_task(
            self, *, split: str | None, index: int | None, task_id: str | None
        ) -> TaskSpec:
            if task_id is not None:
                return self._tasks.get(task_id)
            split_name = split or (self.list_splits()[0] if self.list_splits() else None)
            if split_name is None:
                msg = "this environment has no tasks"
                raise ValueError(msg)
            return self._tasks.split(split_name)[index or 0]

        @staticmethod
        def _task_summary(task: TaskSpec, index: int) -> dict[str, Any]:
            return {
                "id": task.id,
                "index": index,
                "world": task.world,
                "version": task.version,
                "category": task.category,
                "difficulty": task.difficulty,
                "instruction": task.instruction,
                "max_steps": task.max_steps,
                "allowed_tools": task.allowed_tools,
                "tags": task.tags,
            }

    SandboxEnvironment.Action = ToolCallAction
    SandboxEnvironment.Observation = SandboxObservation
    SandboxEnvironment.State = SandboxState
    return SandboxEnvironment


def action_class() -> type[Any]:
    """The action type OpenEnv clients send: ``ToolCallAction(tool=..., args=...)``."""
    action: type[Any] = environment_class().Action
    return action


def make_environment(world: str | WorldSpec, tasks: TaskSource, **kwargs: Any) -> Any:
    """Build an OpenEnv environment for a world and a set of tasks."""
    return environment_class()(world, tasks, **kwargs)


def openenv_available() -> bool:
    """Whether the optional extra is installed, for test skips and the CLI."""
    try:
        _import_openenv()
    except ImportError:
        return False
    return True


__all__ = [
    "INSTALL_HINT",
    "OPENENV_VERSION",
    "action_class",
    "environment_class",
    "make_environment",
    "openenv_available",
]
