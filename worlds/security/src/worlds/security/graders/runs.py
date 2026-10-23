"""Golden and wrong runs: scripted tool calls, kept as data beside the graders.

The security world's copy of the payments run player (decision 86): the same
format, because worlds may not import each other. Every task has a run file,
``graders/runs/<task-id>.yaml``::

    task: triage-travel-login
    runs:
      golden:
        steps:
          - {tool: get_alert, args: {alert_id: ALR-2101}, save: alert}
          - tool: update_ticket
            args: {ticket_id: $alert.ticket.id, classification: benign_positive, status: closed}
      wrong_disables_the_traveller:
        fails_with: security.false_containment
        steps:
          - {tool: disable_account, args: {user_id: meera.s, reason: new country}}

``golden`` must pass its task's grader; every ``wrong_*`` run must fail with
its ``fails_with`` failure type. An argument written ``$name.path`` is replaced
by that path in the result of the step saved as ``name``, for ids only known
once a run reaches them (response action ids). Each step must be accepted
unless it says ``expect_error: <code>``, and ``expect`` must be a subset of its
data, so a run that stops doing what it claims fails loudly instead of failing
the grader for the wrong reason.

:func:`play` drives :func:`kit.scripted.run_script` and grades through the
task's own ``grader:`` path, as for any world.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, overload

import yaml
from pydantic import Field, model_validator

from kit.clock import EPOCH, SimClock
from kit.episode import BaseEpisode
from kit.rng import SeededRNG
from kit.scripted import ScriptRun, run_script
from kit.types import ID_RE, Strict, TaskSpec, ToolCall

from .. import world_spec

RUNS_DIR = Path(__file__).parent / "runs"
GOLDEN = "golden"
WRONG_PREFIX = "wrong_"
REF_PREFIX = "$"

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class RunError(AssertionError):
    """A run file does not do what it says: its script, not the grader, is wrong."""


class Step(Strict):
    """One tool call in a scripted run."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    save: Name | None = None
    """Keep this step's result under a name, for ``$name.path`` arguments later."""
    expect: dict[str, Any] | None = None
    """Fields the step's data must contain: dicts compared as subsets, lists item by item."""
    expect_error: str | None = None
    """The refusal code this step must get."""


class Run(Strict):
    """A scripted run: golden if ``fails_with`` is empty, otherwise a plausible mistake."""

    steps: list[Step] = Field(min_length=1)
    fails_with: str | None = None

    @model_validator(mode="after")
    def _refs_point_back(self) -> Run:
        saved: set[str] = set()
        for number, step in enumerate(self.steps, start=1):
            for ref in _refs(step.args):
                if ref.split(".", 1)[0] not in saved:
                    msg = f"step {number} uses ${ref}, but no earlier step saved that name"
                    raise ValueError(msg)
            if step.save is not None:
                if step.save in saved:
                    msg = f"step {number} saves {step.save!r} a second time"
                    raise ValueError(msg)
                saved.add(step.save)
        return self


class RunFile(Strict):
    """Every scripted run for one task."""

    task: str = Field(pattern=ID_RE.pattern)
    runs: dict[Name, Run]

    @model_validator(mode="after")
    def _golden_and_wrong(self) -> RunFile:
        if GOLDEN not in self.runs or self.runs[GOLDEN].fails_with is not None:
            msg = f"{self.task}: needs a 'golden' run with no fails_with"
            raise ValueError(msg)
        wrong = [name for name in self.runs if name != GOLDEN]
        if not wrong:
            msg = f"{self.task}: needs at least one wrong_* run"
            raise ValueError(msg)
        for name in wrong:
            if not name.startswith(WRONG_PREFIX) or self.runs[name].fails_with is None:
                msg = f"{self.task}: run {name!r} must be named wrong_* and give fails_with"
                raise ValueError(msg)
        return self


def _refs(value: Any) -> list[str]:
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        return [value[len(REF_PREFIX) :]]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _refs(item)]
    return []


def load_runs(task_id: str, directory: Path = RUNS_DIR) -> RunFile:
    """Read one task's run file."""
    path = directory / f"{task_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    runs = RunFile.model_validate(raw)
    if runs.task != task_id:
        msg = f"{path} is for task {runs.task!r}, not {task_id!r}"
        raise ValueError(msg)
    return runs


class _Calls(Sequence[ToolCall]):
    """A run's tool calls, each built only when the episode reaches it.

    ``run_script`` iterates the calls in order and steps the episode between
    them, so by the time call ``n`` is built, every earlier call's result is in
    the episode's trace and its ``$refs`` can be filled in.
    """

    def __init__(self, run: Run, episode: BaseEpisode) -> None:
        self._run = run
        self._episode = episode
        self._saved = {step.save: n for n, step in enumerate(run.steps) if step.save is not None}

    def __len__(self) -> int:
        return len(self._run.steps)

    @overload
    def __getitem__(self, index: int) -> ToolCall: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[ToolCall]: ...

    def __getitem__(self, index: int | slice) -> ToolCall | Sequence[ToolCall]:
        if isinstance(index, slice):
            msg = "scripted calls are built one at a time"
            raise TypeError(msg)
        step = self._run.steps[index]
        return ToolCall(tool=step.tool, args=self._resolve(step.args))

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        if isinstance(value, str) and value.startswith(REF_PREFIX):
            return self._lookup(value[len(REF_PREFIX) :])
        return value

    def _lookup(self, ref: str) -> Any:
        name, *path = ref.split(".")
        step = self._episode.trace.tool_calls()[self._saved[name]]
        if step.response is None:
            msg = f"${ref}: step {name!r} was refused ({step.error}), so it has no result"
            raise RunError(msg)
        found: Any = step.response
        for part in path:
            try:
                found = found[int(part)] if isinstance(found, list) else found[part]
            except (KeyError, IndexError, ValueError, TypeError):
                msg = f"${ref}: {part!r} is not in the result of step {name!r}"
                raise RunError(msg) from None
        return found


def play(task: TaskSpec, run: Run) -> ScriptRun:
    """Run a scripted run on a fresh episode, grade it, and check it went as scripted."""
    spec = world_spec()
    episode = spec.make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
    assert isinstance(episode, BaseEpisode)
    result = run_script(spec, task, _Calls(run, episode), episode=episode)
    _check_as_scripted(run, result)
    return result


def _check_as_scripted(run: Run, result: ScriptRun) -> None:
    if result.steps != len(run.steps):
        msg = f"the episode ended after step {result.steps} of {len(run.steps)}"
        raise RunError(msg)
    for number, (step, outcome) in enumerate(zip(run.steps, result.results, strict=True), 1):
        error = outcome.observation.error
        where = f"step {number} ({step.tool})"
        if step.expect_error is not None:
            if error is None or error.code != step.expect_error:
                got = "accepted" if error is None else error.code
                msg = f"{where}: expected refusal {step.expect_error}, got {got}"
                raise RunError(msg)
            continue
        if error is not None:
            msg = f"{where} was refused: {error.code}: {error.message}"
            raise RunError(msg)
        if step.expect is not None and not _contains(outcome.observation.data, step.expect):
            msg = f"{where}: expected {step.expect}, got {outcome.observation.data}"
            raise RunError(msg)


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_contains(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return bool(actual == expected)


__all__ = ["GOLDEN", "RUNS_DIR", "Run", "RunError", "RunFile", "Step", "load_runs", "play"]
