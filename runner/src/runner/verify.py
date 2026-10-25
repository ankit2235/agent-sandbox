"""``sandbox verify --run <dir>``: replay recorded actions and confirm the same grades.

For every completed run, the tool calls in its trace are applied, in order and
unchanged, to a fresh world built from the same task and seed, with
:func:`kit.scripted.run_script`. No model is involved. The run reproduces if:

- the task is the one that ran (its hash matches the record);
- the replay applied every recorded call;
- every tool call and world event of the replay matches the recording (tool,
  arguments, response, refusal, rows changed, simulated time);
- the grade (passed, score, every check, failure type) is identical;
- with a guard: the decision log's chain is intact and ends at the head ``result.json``
  keeps, and its allowed decisions match the guarded calls in the trace one for one
  (:func:`runner.guarding.verify_decisions`).

:func:`verify_isolated` runs the same replay in a locked-down container
(:mod:`runner.isolation`), with the run directory, and any task directory, policy file or
hidden set the run needs, mounted read-only and nothing writable. The verdicts come back
as JSON (``sandbox verify --json``) and are checked before they are believed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from kit.scripted import run_script
from kit.tasks import HIDDEN_TASKS_ENV, hidden_tasks_dir
from kit.trace import Trace
from kit.types import ToolCall
from kit.world import worlds

from .guarding import DECISIONS_FILE, verify_decisions
from .isolation import (
    HIDDEN_MOUNT,
    RUN_MOUNT,
    EpisodeFailed,
    Isolation,
    Mount,
    container_path,
    run_contained,
)
from .runs import (
    HIDDEN_SUFFIX,
    RUNS_DIR,
    TRACE_FILE,
    load_records,
    read_manifest,
    resolve_task_set,
    task_hash,
    task_set_version,
)


@dataclass(slots=True)
class Verdict:
    """Whether one run reproduced, and what differed if not."""

    run_id: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing differed."""
        return not self.problems


@dataclass(slots=True)
class VerifyReport:
    """Every completed run's verdict, and the runs that could not be checked."""

    verdicts: list[Verdict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    """Errored runs: never graded, so there is nothing to reproduce."""

    @property
    def ok(self) -> bool:
        """True when every completed run reproduced."""
        return all(verdict.ok for verdict in self.verdicts)


def _world_steps(trace: Trace) -> list[dict[str, Any]]:
    """What the world did, without step numbers (agent messages shift them) or the run id."""
    return [step.model_dump(mode="json", exclude={"index"}) for step in trace.grader_view().steps]


def verify(run_dir: Path) -> VerifyReport:
    """Replay every completed run in a run directory."""
    manifest = read_manifest(run_dir)
    if manifest is None:
        msg = f"{run_dir} is not a run directory: no manifest.json"
        raise FileNotFoundError(msg)
    spec = worlds.get(manifest.config.world)
    tasks = {task.id: task for task in resolve_task_set(spec, manifest.config.tasks)}
    report = VerifyReport()
    for record in load_records(run_dir):
        if record.status != "completed" or record.grade is None:
            report.skipped.append(record.run_id)
            continue
        verdict = Verdict(record.run_id)
        report.verdicts.append(verdict)
        task = tasks.get(record.task_id)
        if task is None or task_hash(task) != record.task_hash:
            verdict.problems.append(f"task {record.task_id} is missing or changed since the run")
            continue
        recorded = Trace.read_jsonl(run_dir / RUNS_DIR / record.run_id / TRACE_FILE)
        calls = [
            ToolCall(tool=step.tool or "", args=step.args or {}) for step in recorded.tool_calls()
        ]
        replay = run_script(spec, task, calls, seed=record.seed)
        if replay.steps != len(calls):
            verdict.problems.append(
                f"the replay ended after {replay.steps} of {len(calls)} recorded calls"
            )
        before, after = _world_steps(recorded), _world_steps(replay.trace)
        if before != after:
            first = next(
                (n for n, (a, b) in enumerate(zip(before, after, strict=False)) if a != b),
                min(len(before), len(after)),
            )
            verdict.problems.append(
                f"world steps differ from step {first + 1} "
                f"({len(before)} recorded, {len(after)} replayed)"
            )
        guard = manifest.config.guard
        if guard is not None:
            verdict.problems += verify_decisions(
                run_dir / RUNS_DIR / record.run_id / DECISIONS_FILE,
                record.guard,
                recorded,
                guard,
                spec.name,
            )
        if replay.grade is None or replay.grade.model_dump() != record.grade.model_dump():
            got = replay.grade.model_dump() if replay.grade else None
            verdict.problems.append(
                f"grade differs: recorded {record.grade.model_dump()}, got {got}"
            )
    return report


class _VerdictJson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    problems: list[str]


class _ReportJson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[_VerdictJson]
    skipped: list[str]


def report_json(report: VerifyReport) -> str:
    """A report as the JSON ``sandbox verify --json`` prints."""
    payload = _ReportJson(
        verdicts=[_VerdictJson(run_id=v.run_id, problems=v.problems) for v in report.verdicts],
        skipped=report.skipped,
    )
    return payload.model_dump_json()


def verify_isolated(run_dir: Path, isolation: Isolation) -> VerifyReport:
    """:func:`verify` in a locked-down container, with read-only mounts only.

    Raises :class:`runner.isolation.EpisodeFailed` if the container fails or prints
    anything but a report, and :class:`FileNotFoundError` for a directory that is not a
    run directory.
    """
    manifest = read_manifest(run_dir)
    if manifest is None:
        msg = f"{run_dir} is not a run directory: no manifest.json"
        raise FileNotFoundError(msg)
    config = manifest.config
    spec = worlds.get(config.world)
    mounts = [Mount(run_dir.resolve(), RUN_MOUNT)]
    env: dict[str, str] = {}
    if config.tasks == task_set_version(spec) + HIDDEN_SUFFIX:
        hidden = hidden_tasks_dir()
        if hidden is None:
            msg = f"{config.tasks} runs need {HIDDEN_TASKS_ENV} to be set"
            raise FileNotFoundError(msg)
        mounts.append(Mount(hidden.resolve(), HIDDEN_MOUNT))
        env[HIDDEN_TASKS_ENV] = HIDDEN_MOUNT
    elif Path(config.tasks).is_dir():
        mounts.append(Mount(Path(config.tasks).resolve(), container_path(config.tasks)))
    if config.guard is not None and Path(config.guard.policy).is_file():
        policy = config.guard.policy
        mounts.append(Mount(Path(policy).resolve(), container_path(policy)))
    done = run_contained(
        isolation, ["verify", "--run", RUN_MOUNT, "--json"], mounts=mounts, env=env
    )
    if done.returncode not in (0, 1):
        msg = f"verify exited with status {done.returncode} in its container: {done.stderr}"
        raise EpisodeFailed(msg.strip())
    try:
        parsed = _ReportJson.model_validate_json(done.stdout)
    except ValidationError:
        msg = f"verify's container did not print a report: {done.stderr or 'no message'}"
        raise EpisodeFailed(msg) from None
    return VerifyReport(
        verdicts=[Verdict(v.run_id, list(v.problems)) for v in parsed.verdicts],
        skipped=list(parsed.skipped),
    )


__all__ = ["Verdict", "VerifyReport", "report_json", "verify", "verify_isolated"]
