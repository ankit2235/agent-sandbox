"""Running a fixed list of tool calls against a world.

This is how golden and wrong runs are executed: no model, no network, just a
script. CI uses it for every task in every world, and the determinism tests
compare its output byte for byte.

Run files (decision 86, moved here from the worlds in Phase 4) keep those
scripts as data, one per task at ``<runs_dir>/<task-id>.yaml``::

    task: commerce-price-rises-above-budget
    runs:
      golden:
        steps:
          - {tool: checkout, args: {merchant_id: drift-goods}, save: co}
          - {tool: ask_user, args: {question: "..."}}
      wrong_pays_above_budget:
        fails_with: payments.overspend
        steps:
          - {tool: checkout, args: {merchant_id: drift-goods}, save: co}
          - tool: pay
            args: {order_id: $co.order.id, payee: $co.payment_request.payee, ...}
            expect: {outcome: paid}

``golden`` must pass its task's grader; every ``wrong_*`` run must fail with
its ``fails_with`` failure type. Ids are often seeded, known only once a run
reaches them, so an argument written ``$name.path`` is replaced by that path in
the result of the step saved as ``name`` (:func:`resolve_refs`). Each step must
be accepted unless it says ``expect_error: <code>``, and ``expect`` must be a
subset of its data, so a run that stops doing what it claims fails loudly
instead of failing the grader for the wrong reason.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, overload

import yaml
from pydantic import Field, model_validator

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.trace import Trace
from kit.types import ID_RE, GradeResult, Observation, StepResult, Strict, TaskSpec, ToolCall
from kit.world import Episode, WorldSpec, load_grader

GOLDEN = "golden"
WRONG_PREFIX = "wrong_"
REF_PREFIX = "$"

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


@dataclass(slots=True)
class ScriptRun:
    """Everything one scripted episode produced."""

    task: TaskSpec
    trace: Trace
    first_observation: Observation
    results: list[StepResult] = field(default_factory=list)
    grade: GradeResult | None = None
    final_state: dict[str, bytes] = field(default_factory=dict)

    @property
    def steps(self) -> int:
        """How many tool calls were applied."""
        return len(self.results)

    @property
    def passed(self) -> bool:
        """Whether the grader passed the run."""
        return bool(self.grade and self.grade.passed)

    @property
    def score(self) -> float:
        """The grader's score, or 0.0 if the run was not graded."""
        return self.grade.score if self.grade else 0.0


def run_script(
    spec: WorldSpec,
    task: TaskSpec,
    calls: Sequence[ToolCall],
    *,
    seed: int | None = None,
    start: dt.datetime = EPOCH,
    grade: bool = True,
    episode: Episode | None = None,
) -> ScriptRun:
    """Apply ``calls`` to a fresh episode, then grade it.

    Stops early when the episode reports done, which is how a step cap or a
    terminal tool ends a run.
    """
    clock = SimClock(start)
    rng = SeededRNG(seed if seed is not None else task.seed)
    active: Any = episode if episode is not None else spec.make_episode(task, clock, rng)
    first = active.reset(task)
    run = ScriptRun(task=task, trace=active.trace, first_observation=first)
    for call in calls:
        result = active.step(call)
        run.results.append(result)
        if result.done:
            break
    if grade:
        grader = load_grader(task.grader)
        run.grade = grader(active.state, active.trace.grader_view(), task)
    run.final_state = _snapshot(active)
    active.close()
    return run


def _snapshot(episode: Any) -> dict[str, bytes]:
    snapshot = getattr(episode, "snapshot_all", None)
    if callable(snapshot):
        result: dict[str, bytes] = snapshot()
        return result
    return {}


# ------------------------------------------------------------------ run files
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

    @property
    def wrong(self) -> list[str]:
        """The wrong runs' names, in file order."""
        return [name for name in self.runs if name != GOLDEN]


def _refs(value: Any) -> list[str]:
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        return [value[len(REF_PREFIX) :]]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _refs(item)]
    return []


def load_runs(task_id: str, directory: str | Path) -> RunFile:
    """Read one task's run file from a world's runs directory."""
    path = Path(directory) / f"{task_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    runs = RunFile.model_validate(raw)
    if runs.task != task_id:
        msg = f"{path} is for task {runs.task!r}, not {task_id!r}"
        raise ValueError(msg)
    return runs


def resolve_refs(value: Any, saved: Mapping[str, Any]) -> Any:
    """Replace every ``$name.path`` in ``value`` with that path in ``saved[name]``.

    ``saved`` maps a saved step's name to its result, or to ``None`` if the
    step was refused. Raises :class:`RunError` when a path is not there.
    """
    if isinstance(value, dict):
        return {key: resolve_refs(item, saved) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, saved) for item in value]
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        return _lookup(value[len(REF_PREFIX) :], saved)
    return value


def _lookup(ref: str, saved: Mapping[str, Any]) -> Any:
    name, *path = ref.split(".")
    if name not in saved:
        msg = f"${ref}: no step saved as {name!r} has run yet"
        raise RunError(msg)
    found = saved[name]
    if found is None:
        msg = f"${ref}: step {name!r} was refused, so it has no result"
        raise RunError(msg)
    for part in path:
        try:
            found = found[int(part)] if isinstance(found, list) else found[part]
        except (KeyError, IndexError, ValueError, TypeError):
            msg = f"${ref}: {part!r} is not in the result of step {name!r}"
            raise RunError(msg) from None
    return found


class _Calls(Sequence[ToolCall]):
    """A run's tool calls, each built only when the episode reaches it.

    ``run_script`` iterates the calls in order and steps the episode between
    them, so by the time call ``n`` is built, every earlier call's result is in
    the episode's trace and its ``$refs`` can be filled in.
    """

    def __init__(self, run: Run, episode: Episode) -> None:
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
        calls = self._episode.trace.tool_calls()
        saved = {name: calls[n].response for name, n in self._saved.items() if n < len(calls)}
        return ToolCall(tool=step.tool, args=resolve_refs(step.args, saved))


def play(spec: WorldSpec, task: TaskSpec, run: Run) -> ScriptRun:
    """Run a scripted run on a fresh episode, grade it, and check it went as scripted."""
    episode = spec.make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
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


__all__ = [
    "GOLDEN",
    "Run",
    "RunError",
    "RunFile",
    "ScriptRun",
    "Step",
    "load_runs",
    "play",
    "resolve_refs",
    "run_script",
]
