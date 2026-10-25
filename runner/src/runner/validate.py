"""Checks shared by ``sandbox tasks validate``, ``sandbox hidden check`` and ``release-check``.

- :func:`validate_file`: everything one task file must pass (the kit's checks, the world's
  whole-file JSON Schema, its name and its directory);
- :func:`play_runs`: a task's golden run passes and each wrong run fails with the failure
  type it declares (CONTRIBUTING rule 4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kit.scripted import Run, RunError, RunFile, ScriptRun, play
from kit.tasks import load_task
from kit.types import TaskSpec
from kit.world import WorldSpec, load_grader, worlds

TASK_FILE_SCHEMA = "schema.json"
"""A world may ship a JSON Schema for whole task files beside its tasks."""


def task_files(directory: Path) -> list[Path]:
    """Every task file under a directory."""
    return sorted(directory.glob("**/*.y*ml"))


def world_schema(spec: WorldSpec) -> dict[str, Any] | None:
    """A world's whole-file task schema, if it ships one as ``<tasks_dir>/schema.json``."""
    if spec.tasks_dir is None:
        return None
    path = spec.tasks_dir / TASK_FILE_SCHEMA
    if not path.exists():
        return None
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def validate_file(path: Path, root: Path) -> TaskSpec:
    """Every check one task file must pass. Raises on the first problem."""
    import jsonschema

    task = load_task(path)
    spec = worlds.get(task.world)
    spec.validate_task(task)
    load_grader(task.grader)
    schema = world_schema(spec)
    if schema is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        # Validate the file as JSON sees it: YAML turns 2026-10-18 into a date object.
        jsonschema.validate(json.loads(json.dumps(raw, default=str)), schema)
    if path.stem != task.id:
        msg = f"file name {path.name} does not match the task id {task.id!r}"
        raise ValueError(msg)
    if path.parent != root and path.parent.name != task.category:
        msg = f"a {task.category!r} task must live in a {task.category}/ directory"
        raise ValueError(msg)
    return task


def played(spec: WorldSpec, task: TaskSpec, run: Run) -> ScriptRun | None:
    """The run played, or None if a step did not go as scripted (it was refused)."""
    try:
        return play(spec, task, run)
    except RunError:
        return None


@dataclass(slots=True)
class RunsPlayed:
    """How many of a task's runs were played, and what did not behave as declared."""

    runs: int = 0
    problems: list[str] = field(default_factory=list)


def play_runs(spec: WorldSpec, task: TaskSpec, runs: RunFile) -> RunsPlayed:
    """Play the golden run and every wrong run; each must end as it declares."""
    result = RunsPlayed()
    golden = played(spec, task, runs.runs["golden"])
    result.runs += 1
    if golden is None or not golden.passed:
        result.problems.append(f"{task.id}: the golden run does not pass as scripted")
    for name in runs.wrong:
        trap = runs.runs[name]
        outcome = played(spec, task, trap)
        result.runs += 1
        got = outcome.grade.failure_type if outcome is not None and outcome.grade else None
        if got != trap.fails_with:
            result.problems.append(f"{task.id}: {name} fails with {got}, not {trap.fails_with}")
    return result


__all__ = [
    "TASK_FILE_SCHEMA",
    "RunsPlayed",
    "play_runs",
    "played",
    "task_files",
    "validate_file",
    "world_schema",
]
