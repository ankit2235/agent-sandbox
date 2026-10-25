"""Leaderboard files, in the leaderboard's data format (``results-v0.1``).

``sandbox export <run-dir>... --out <data-dir> --updated YYYY-MM-DD`` writes::

    worlds.json                       one entry per world; entries for worlds not
                                      being exported are kept as they were
    <world>/results.json              one entry per model
    <world>/tasks.json                public tasks only
    <world>/traces/<run_id>.json      public runs only

Every document is checked against its JSON Schema in ``runner/schemas/`` before
anything is written. Hidden tasks never appear by id: their runs only feed the
``hidden`` aggregate of a model's metrics.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from kit.failures import failure_types
from kit.trace import Trace
from kit.types import TaskSpec
from kit.world import WorldSpec, worlds

from .runs import (
    RUNS_DIR,
    TRACE_FILE,
    Manifest,
    RunRecord,
    extra_set,
    load_records,
    read_manifest,
    resolve_task_set,
    task_hash,
    write_json,
)
from .score import COMPUTED, failure_key, hidden_metrics, score

SCHEMAS = "runner.schemas"
WORLDS_FILE = "worlds.json"


def schema(name: str) -> dict[str, Any]:
    """One of the leaderboard JSON Schemas: ``worlds``, ``results``, ``tasks`` or ``trace``."""
    text = (resources.files(SCHEMAS) / f"{name}.schema.json").read_text(encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def validate(name: str, document: Any) -> None:
    """Raise ``jsonschema.ValidationError`` if a document does not match its schema."""
    jsonschema.Draft202012Validator(schema(name)).validate(document)


@dataclass(slots=True)
class RunDir:
    """One ``sandbox run`` output directory, loaded."""

    path: Path
    manifest: Manifest
    records: list[RunRecord]


def load_run_dir(path: Path) -> RunDir:
    """Read a run directory's manifest and records."""
    manifest = read_manifest(path)
    if manifest is None:
        msg = f"{path} is not a run directory: no manifest.json"
        raise FileNotFoundError(msg)
    return RunDir(path=path, manifest=manifest, records=load_records(path))


def task_title(task: TaskSpec) -> str:
    """A readable title from the id: ``rails-gym-autopay-mandate`` -> ``Gym autopay mandate``."""
    words = task.id.removeprefix(f"{task.category}-").replace("_", "-").split("-")
    text = " ".join(word for word in words if word)
    return text[:1].upper() + text[1:]


def world_entry(spec: WorldSpec) -> dict[str, Any]:
    """A world's ``worlds.json`` entry, from its WorldSpec and listing."""
    listing = spec.listing
    if listing is None:
        msg = f"world {spec.name!r} has no leaderboard listing (WorldSpec.listing)"
        raise ValueError(msg)
    unknown = [metric.key for metric in listing.metrics if metric.key not in COMPUTED]
    if unknown:
        msg = f"{spec.name} lists metrics the runner does not compute: {unknown}"
        raise ValueError(msg)
    kinds = [*spec.failure_types, *(failure_types.get(key) for key in failure_types.ids("kit"))]
    keys = [failure_key(kind.id) for kind in kinds]
    if len(set(keys)) != len(keys):
        msg = f"{spec.name} has a failure type whose key collides with a kit one: {keys}"
        raise ValueError(msg)
    return {
        "id": spec.name,
        "name": listing.title,
        "version": f"{spec.name}-{spec.version}",
        "status": listing.status,
        "tagline": listing.tagline,
        "description": spec.description,
        "disclaimer": listing.disclaimer,
        "categories": [
            {"key": category, "label": spec.category_label(category)}
            for category in spec.categories
        ],
        "metrics": [metric.model_dump(exclude_defaults=True) for metric in listing.metrics],
        "failure_types": [
            {"key": key, "label": kind.label} for key, kind in zip(keys, kinds, strict=True)
        ],
        "tools": list(spec.tool_names),
    }


def trace_document(record: RunRecord, trace: Trace, model_id: str) -> dict[str, Any]:
    """A run's trace in the leaderboard format.

    One step per tool call or world event. The model's text goes on the first
    tool call of its turn; text from a turn that applied no tool call (its final
    answer) becomes a step of its own, with ``tool: null``.
    """
    grade = record.grade
    if grade is None:
        msg = f"{record.run_id} was not graded, so it has no public trace"
        raise ValueError(msg)
    steps: list[dict[str, Any]] = []
    message: str | None = None

    def add(**fields: Any) -> None:
        steps.append({"n": len(steps) + 1, **fields})

    for step in trace.steps:
        sim_time = step.sim_time.isoformat()
        if step.kind == "agent_message":
            if message:
                add(sim_time=sim_time, agent_message=message, tool=None, args=None,
                    response=None, state_diff=[])  # fmt: skip
            message = step.agent_message or None
        elif step.kind == "tool_call":
            response = (
                {"error": step.error.model_dump(mode="json")} if step.error else step.response
            )
            add(
                sim_time=sim_time,
                agent_message=message,
                tool=step.tool,
                args=step.args,
                response=response,
                state_diff=[entry.model_dump(mode="json") for entry in step.state_diff],
            )
            message = None
        elif step.kind == "event":
            add(
                sim_time=sim_time,
                agent_message=None,
                tool=None,
                args=None,
                response={"event": step.event, "payload": step.args or {}},
                state_diff=[entry.model_dump(mode="json") for entry in step.state_diff],
            )
    if message:
        last = trace.steps[-1].sim_time.isoformat()
        add(
            sim_time=last, agent_message=message, tool=None, args=None, response=None, state_diff=[]
        )
    return {
        "run_id": record.run_id,
        "world": record.world,
        "model_id": model_id,
        "task_id": record.task_id,
        "passed": grade.passed,
        "fail_reason": fail_reason(record),
        "failure_type": failure_key(grade.failure_type) if grade.failure_type else None,
        "steps": steps,
    }


def fail_reason(record: RunRecord) -> str | None:
    """One line on why a run failed: the failed checks, or why it stopped."""
    grade = record.grade
    if grade is None or grade.passed:
        return None
    failed = "; ".join(f"{check.name}: {check.detail}" for check in grade.failed_checks)
    stop = f"stopped: {record.stop_reason}"
    return f"{failed} ({stop})" if failed else stop


@dataclass(slots=True)
class WorldExport:
    """The documents for one world, built and validated, not yet written."""

    results: dict[str, Any]
    tasks: list[dict[str, Any]]
    traces: dict[str, dict[str, Any]]


def build_world(spec: WorldSpec, run_dirs: Sequence[RunDir], updated: dt.date) -> WorldExport:
    """results.json, tasks.json and the traces for one world from its run directories."""
    version = f"{spec.name}-{spec.version}"
    by_model: dict[str, list[RunDir]] = defaultdict(list)
    for run_dir in run_dirs:
        if run_dir.manifest.version != version:
            msg = f"{run_dir.path} is {run_dir.manifest.version}; {spec.name} is now {version}"
            raise ValueError(msg)
        if extra_set(spec, run_dir.manifest.config.tasks) is not None:
            msg = (
                f"{run_dir.path} ran {run_dir.manifest.config.tasks}, a pre-release task set "
                f"that is not part of {version}'s results; it is not exported (DECISIONS 266)"
            )
            raise ValueError(msg)
        by_model[run_dir.manifest.config.model_slug].append(run_dir)

    public_tasks: dict[str, TaskSpec] = {}
    models: list[dict[str, Any]] = []
    traces: dict[str, dict[str, Any]] = {}
    passes: dict[str, list[bool]] = defaultdict(list)
    checks: dict[str, list[str]] = {}
    for model_id, dirs in sorted(by_model.items()):
        records = _model_records(model_id, dirs)
        for run_dir in dirs:
            for task in _tasks_of(spec, run_dir):
                if task.visibility == "public":
                    public_tasks[task.id] = task
        public = [r for r in records if r.visibility == "public" and r.status == "completed"]
        hidden = [r for r in records if r.visibility == "hidden" and r.status == "completed"]
        if not public:
            msg = f"model {model_id} has no completed public runs for {spec.name}"
            raise ValueError(msg)
        scores = score(spec, public)
        metrics: dict[str, Any] = {"public": scores.metrics}
        if hidden:
            metrics["hidden"] = hidden_metrics(score(spec, hidden))
        config = dirs[0].manifest.config
        models.append(
            {
                "id": model_id,
                "name": config.model_name,
                "provider": config.provider,
                "open_weight": config.open_weight,
                "runs": max(run_dir.manifest.config.runs for run_dir in dirs),
                "metrics": metrics,
                "by_category": scores.by_category,
                "failures": scores.failures,
                "task_results": [
                    {"task_id": r.task_id, "passed": r.passed, "run_id": r.run_id} for r in public
                ],
            }
        )
        for record in public:
            passes[record.task_id].append(record.passed)
            if record.grade is not None:
                checks.setdefault(record.task_id, [check.name for check in record.grade.checks])
            trace = Trace.read_jsonl(_run_path(dirs, record) / TRACE_FILE)
            traces[record.run_id] = trace_document(record, trace, model_id)

    results = {"world": spec.name, "version": version, "updated": updated.isoformat(),
               "models": models}  # fmt: skip
    tasks = [
        {
            "id": task.id,
            "title": task_title(task),
            "category": task.category,
            "difficulty": task.difficulty,
            "instruction": task.instruction,
            "initial_state": task.model_dump(mode="json")["initial_state"],
            "grader_checks": checks.get(task.id, []),
            "pass_rate": (
                round(100 * sum(passes[task.id]) / len(passes[task.id]), 1)
                if passes.get(task.id)
                else None
            ),
        }
        for task in sorted(public_tasks.values(), key=lambda task: task.id)
    ]
    export = WorldExport(results=results, tasks=tasks, traces=traces)
    _check_no_hidden(export, run_dirs)
    validate("results", export.results)
    validate("tasks", export.tasks)
    for document in export.traces.values():
        validate("trace", document)
    return export


def _model_records(model_id: str, dirs: Sequence[RunDir]) -> list[RunRecord]:
    records: list[RunRecord] = []
    seen: set[str] = set()
    for run_dir in dirs:
        for record in run_dir.records:
            if record.run_id in seen:
                msg = f"run {record.run_id} of {model_id} is in more than one run directory"
                raise ValueError(msg)
            seen.add(record.run_id)
            records.append(record)
    return records


def _tasks_of(spec: WorldSpec, run_dir: RunDir) -> list[TaskSpec]:
    """The run directory's tasks, checked against the hashes its records carry."""
    listed = set(run_dir.manifest.tasks)
    tasks = [t for t in resolve_task_set(spec, run_dir.manifest.config.tasks) if t.id in listed]
    hashes = {task.id: task_hash(task) for task in tasks}
    for record in run_dir.records:
        if hashes.get(record.task_id) != record.task_hash:
            msg = f"task {record.task_id} changed since {record.run_id} ran"
            raise ValueError(msg)
    return tasks


def _run_path(dirs: Sequence[RunDir], record: RunRecord) -> Path:
    for run_dir in dirs:
        path = run_dir.path / RUNS_DIR / record.run_id
        if path.exists():
            return path
    msg = f"no run directory holds {record.run_id}"
    raise FileNotFoundError(msg)


def _check_no_hidden(export: WorldExport, run_dirs: Sequence[RunDir]) -> None:
    """Belt and braces: no hidden task id or hidden run id in any public document."""
    hidden_tasks = {r.task_id for d in run_dirs for r in d.records if r.visibility == "hidden"}
    hidden_runs = {r.run_id for d in run_dirs for r in d.records if r.visibility == "hidden"}
    leaked = {task["id"] for task in export.tasks} & hidden_tasks
    for model in export.results["models"]:
        leaked |= {row["task_id"] for row in model["task_results"]} & hidden_tasks
        leaked |= {row["run_id"] for row in model["task_results"]} & hidden_runs
    leaked |= set(export.traces) & hidden_runs
    if leaked:
        msg = f"hidden tasks would be published: {sorted(leaked)}"
        raise AssertionError(msg)


def export(run_paths: Sequence[Path], out: Path, updated: dt.date) -> list[Path]:
    """Build, validate and write every leaderboard file. Returns the paths written."""
    run_dirs = [load_run_dir(path) for path in run_paths]
    guarded = [
        str(path) for path, d in zip(run_paths, run_dirs, strict=True) if d.manifest.config.guard
    ]
    if guarded:
        msg = (
            f"guarded runs never go on the main leaderboard: {guarded}; "
            "their scores go in guards/results.json (sandbox-train report --runs)"
        )
        raise ValueError(msg)
    grouped: dict[str, list[RunDir]] = defaultdict(list)
    for run_dir in run_dirs:
        grouped[run_dir.manifest.config.world].append(run_dir)
    built = {
        name: (worlds.get(name), build_world(worlds.get(name), dirs, updated))
        for name, dirs in sorted(grouped.items())
    }
    entries = merged_worlds(out / WORLDS_FILE, [world_entry(spec) for spec, _ in built.values()])
    validate("worlds", entries)

    written = [out / WORLDS_FILE]
    write_json(out / WORLDS_FILE, entries)
    for name, (_, documents) in built.items():
        folder = out / name
        write_json(folder / "results.json", documents.results)
        write_json(folder / "tasks.json", documents.tasks)
        written += [folder / "results.json", folder / "tasks.json"]
        traces = folder / "traces"
        traces.mkdir(parents=True, exist_ok=True)
        for stale in traces.glob("*.json"):
            if stale.stem not in documents.traces:
                stale.unlink()
        for run_id, document in sorted(documents.traces.items()):
            write_json(traces / f"{run_id}.json", document)
            written.append(traces / f"{run_id}.json")
    return written


def merged_worlds(path: Path, entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The existing ``worlds.json`` with these entries replaced or appended; others kept."""
    existing: list[dict[str, Any]] = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        validate("worlds", existing)
    fresh = {entry["id"]: entry for entry in entries}
    merged = [fresh.pop(entry["id"], entry) for entry in existing]
    return merged + list(fresh.values())


__all__ = [
    "WORLDS_FILE",
    "RunDir",
    "build_world",
    "export",
    "fail_reason",
    "load_run_dir",
    "merged_worlds",
    "schema",
    "task_title",
    "trace_document",
    "validate",
    "world_entry",
]
