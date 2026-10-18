"""Loading task files.

Public tasks live in ``worlds/<world>/tasks/<category>/<id>.yaml``. Hidden
tasks live in a separate private repository, found through
``HIDDEN_TASKS_DIR``; nothing in this repo may contain one. The YAML maps
one-to-one onto :class:`kit.types.TaskSpec`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml

from kit.types import TaskSpec

HIDDEN_TASKS_ENV = "HIDDEN_TASKS_DIR"


def load_task(path: str | Path) -> TaskSpec:
    """Read one task file."""
    source = Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"{source} must contain a YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)
    return TaskSpec.model_validate(raw)


def load_tasks(directory: str | Path, *, recursive: bool = True) -> list[TaskSpec]:
    """Read every task file under a directory, sorted by id."""
    root = Path(directory)
    if not root.exists():
        msg = f"task directory {root} does not exist"
        raise FileNotFoundError(msg)
    pattern = "**/*.y*ml" if recursive else "*.y*ml"
    tasks = [load_task(path) for path in sorted(root.glob(pattern))]
    return sorted(tasks, key=lambda task: task.id)


def hidden_tasks_dir() -> Path | None:
    """The private task directory, if the environment points at one."""
    value = os.environ.get(HIDDEN_TASKS_ENV)
    if not value:
        return None
    path = Path(value)
    if not path.exists():
        msg = f"{HIDDEN_TASKS_ENV} points at {path}, which does not exist"
        raise FileNotFoundError(msg)
    return path


class TaskSource:
    """A set of tasks, grouped into splits by task-set version.

    The splits are what the OpenEnv adapter exposes, so ``payments-v0.1`` and
    ``payments-v0.1-hidden`` are selectable by name and index.
    """

    def __init__(self, tasks: Iterable[TaskSpec]) -> None:
        self._tasks = sorted(tasks, key=lambda task: (task.version, task.visibility, task.id))
        self._splits: dict[str, list[TaskSpec]] = {}
        for task in self._tasks:
            name = task.version if task.visibility == "public" else f"{task.version}-hidden"
            self._splits.setdefault(name, []).append(task)

    @classmethod
    def from_directories(cls, directories: Sequence[str | Path]) -> TaskSource:
        """Load every task under each directory."""
        tasks: list[TaskSpec] = []
        for directory in directories:
            tasks.extend(load_tasks(directory))
        return cls(tasks)

    @property
    def tasks(self) -> list[TaskSpec]:
        """Every task, in a stable order."""
        return list(self._tasks)

    def splits(self) -> list[str]:
        """Split names, e.g. ``["payments-v0.1"]``."""
        return sorted(self._splits)

    def split(self, name: str) -> list[TaskSpec]:
        """Tasks in one split."""
        try:
            return list(self._splits[name])
        except KeyError:
            msg = f"unknown split {name!r}; available: {self.splits()}"
            raise KeyError(msg) from None

    def get(self, task_id: str) -> TaskSpec:
        """One task by id."""
        for task in self._tasks:
            if task.id == task_id:
                return task
        msg = f"unknown task {task_id!r}"
        raise KeyError(msg)

    def __len__(self) -> int:
        return len(self._tasks)

    def __repr__(self) -> str:
        return f"TaskSource({len(self._tasks)} tasks, splits={self.splits()})"
