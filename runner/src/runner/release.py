"""``sandbox release-check <world>-<version>``: may this task set be released?

Every condition is checked and reported by name, passing or not, with who or what it
waits on, so it is clear which failures are code and which wait for people:

- ``public-tasks``: 50-80 public tasks, every file valid (``sandbox tasks validate``);
- ``hidden-set``: the hidden set loads from ``HIDDEN_TASKS_DIR``, matches its published
  hashes and every run plays as declared (``sandbox hidden check``); counts only;
- ``signoff``: every public task has ``reviewer_signoff``. A hidden task is a seeded
  variant of a public task, so it follows its base task's sign-off;
- ``grader-runs``: every public and stressed task's golden run passes and every wrong
  run fails with the failure type it declares;
- ``frozen``: the public tasks match ``<tasks_dir>/frozen-<version>.json``;
- ``calibration``: a stress calibration report of real models covers every modifier;
- ``results``: at least ``min_models`` real models ran the whole set, each run directory
  passing :func:`runner.submission.check` (complete, reproduced by ``sandbox verify``);
- ``published``: the committed leaderboard files are exactly what those results export;
- ``no-hidden``: no canary, no task marked hidden and no published hidden task in any
  file git tracks.

The run directories themselves are never committed: ``--results`` points outside git.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml

from kit.tasks import hidden_tasks_dir
from kit.types import TaskSpec
from kit.world import WorldSpec, worlds
from stress.core import registry

from .calibrate import REPORT_FORMAT
from .export import RunDir, build_world, load_run_dir
from .hidden import HiddenSetError, check_set, public_leaks, scan
from .models import FAKE_PREFIX, task_runs
from .runs import (
    HIDDEN_SUFFIX,
    STRESSED_SUFFIX,
    read_manifest,
    resolve_task_set,
    task_hash,
    task_set_version,
)
from .submission import CHECK_DATE, check
from .validate import play_runs, task_files, validate_file
from .verify import verify

MIN_TASKS, MAX_TASKS = 50, 80
MIN_MODELS = 5
MIN_RUNS = 3
MIN_CALIBRATION_MODELS = 2
FREEZE_FORMAT = "frozen-tasks-v1"
LEAK_SCRIPT = "scripts/check_no_hidden.py"
SCANNED = ("kit", "runner", "stress", "worlds", "scripts", "docs")
"""Where ``public_leaks`` looks for copies of hidden tasks (as ``just no-hidden`` does)."""

WaitsOn = Literal["code", "reviewer", "real model runs", "HIDDEN_TASKS_DIR", "maintainer"]
REAL_RUNS: WaitsOn = "real model runs"


@dataclass(frozen=True, slots=True)
class Condition:
    """One release condition's verdict."""

    name: str
    passed: bool
    detail: str
    waits_on: WaitsOn = "code"


@dataclass(frozen=True, slots=True)
class GateReport:
    """Every condition, in order."""

    task_set: str
    conditions: list[Condition]

    @property
    def ok(self) -> bool:
        """True when every condition passes."""
        return all(condition.passed for condition in self.conditions)

    @property
    def failing(self) -> list[str]:
        """The names of the failing conditions."""
        return [condition.name for condition in self.conditions if not condition.passed]


def world_of(task_set: str) -> WorldSpec:
    """The installed world whose ``<world>-<version>`` is ``task_set``."""
    for name in worlds.names():
        spec = worlds.get(name)
        if task_set_version(spec) == task_set:
            return spec
    known = [task_set_version(worlds.get(name)) for name in worlds.names()]
    msg = f"no installed world has the task set {task_set!r}; installed: {known}"
    raise ValueError(msg)


def public_tasks(spec: WorldSpec) -> list[TaskSpec]:
    """The world's public tasks of its current version."""
    return [t for t in spec.task_source().split(spec.version) if t.visibility == "public"]


# ------------------------------------------------------------------- freeze
def freeze_file(spec: WorldSpec) -> Path:
    """``<tasks_dir>/frozen-<version>.json``."""
    if spec.tasks_dir is None:
        msg = f"world {spec.name!r} ships no tasks directory"
        raise ValueError(msg)
    return spec.tasks_dir / f"frozen-{spec.version}.json"


def frozen_list(spec: WorldSpec) -> dict[str, Any]:
    """Each public task's hash (``task_hash``, which leaves out the sign-off)."""
    hashes = {task.id: task_hash(task) for task in public_tasks(spec)}
    return {
        "format": FREEZE_FORMAT,
        "set": task_set_version(spec),
        "count": len(hashes),
        "tasks": dict(sorted(hashes.items())),
    }


def write_freeze(spec: WorldSpec) -> Path:
    """Freeze the public tasks' content as it is now."""
    path = freeze_file(spec)
    path.write_text(json.dumps(frozen_list(spec), indent=2) + "\n", encoding="utf-8")
    return path


def freeze_problems(spec: WorldSpec) -> list[str]:
    """How the public tasks differ from their freeze file; empty when they match."""
    path = freeze_file(spec)
    if not path.exists():
        return [f"no freeze file at {path}; run `sandbox freeze {task_set_version(spec)}`"]
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("format") != FREEZE_FORMAT or frozen.get("set") != task_set_version(spec):
        return [f"{path} is not a {FREEZE_FORMAT} list for {task_set_version(spec)}"]
    now: dict[str, str] = frozen_list(spec)["tasks"]
    was: dict[str, str] = frozen["tasks"]
    problems = [f"{task_id} was added" for task_id in sorted(set(now) - set(was))]
    problems += [f"{task_id} was removed" for task_id in sorted(set(was) - set(now))]
    changed = [task_id for task_id in sorted(set(now) & set(was)) if now[task_id] != was[task_id]]
    problems += [f"{task_id} changed" for task_id in changed]
    return problems


# --------------------------------------------------------------- conditions
def _few(items: list[str], limit: int = 3) -> str:
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(items[:limit]) + more


def check_public_tasks(spec: WorldSpec) -> Condition:
    """50-80 public tasks, every task file valid."""
    if spec.tasks_dir is None:
        return Condition("public-tasks", False, "the world ships no tasks directory")
    problems: list[str] = []
    for path in task_files(spec.tasks_dir):
        try:
            validate_file(path, spec.tasks_dir)
        except (ValueError, KeyError, ImportError, AttributeError, yaml.YAMLError) as error:
            problems.append(f"{path.name}: {error}")
        except jsonschema.ValidationError as error:
            problems.append(f"{path.name}: {error.message}")
    count = len(public_tasks(spec))
    if not MIN_TASKS <= count <= MAX_TASKS:
        problems.insert(0, f"{count} public tasks, {MIN_TASKS}-{MAX_TASKS} needed")
    if problems:
        return Condition("public-tasks", False, _few(problems))
    return Condition("public-tasks", True, f"{count} public tasks, every file valid")


def check_hidden_set(spec: WorldSpec) -> Condition:
    """The hidden set, from HIDDEN_TASKS_DIR only. Reports counts, never content."""
    name = "hidden-set"
    if hidden_tasks_dir() is None:
        return Condition(name, False, "HIDDEN_TASKS_DIR is not set", "HIDDEN_TASKS_DIR")
    try:
        report = check_set(spec)
    except HiddenSetError as error:
        return Condition(name, False, str(error), "HIDDEN_TASKS_DIR")
    except (ValueError, KeyError, ImportError, jsonschema.ValidationError) as error:
        detail = (
            f"a hidden task failed validation ({type(error).__name__}); "
            f"run `sandbox hidden check --world {spec.name}` privately for details"
        )
        return Condition(name, False, detail, "maintainer")
    counts = f"{report.tasks} tasks, {report.runs} runs played, {len(report.problems)} problem(s)"
    if report.problems or report.tasks == 0:
        return Condition(name, False, counts, "maintainer")
    return Condition(name, True, counts + "; hashes match the published list")


def check_signoff(spec: WorldSpec) -> Condition:
    """Every public task signed off; hidden tasks follow their base task."""
    tasks = public_tasks(spec)
    unsigned = [task.id for task in tasks if task.reviewer_signoff is None]
    if unsigned:
        detail = f"{len(unsigned)} of {len(tasks)} public tasks have no reviewer_signoff"
        return Condition("signoff", False, detail, "reviewer")
    return Condition("signoff", True, f"all {len(tasks)} public tasks signed off")


def check_grader_runs(spec: WorldSpec) -> Condition:
    """Golden and wrong runs of every public and stressed task play as declared."""
    if spec.runs_dir is None:
        return Condition("grader-runs", False, "the world ships no run files")
    tasks = public_tasks(spec)
    stressed = resolve_task_set(spec, task_set_version(spec) + STRESSED_SUFFIX)
    played = 0
    problems: list[str] = []
    for task in [*tasks, *stressed]:
        try:
            outcome = play_runs(spec, task, task_runs(spec, task))
        except FileNotFoundError:
            problems.append(f"{task.id}: no run file")
            continue
        played += outcome.runs
        problems += outcome.problems
    counts = f"{played} runs of {len(tasks)} public and {len(stressed)} stressed tasks"
    if problems:
        return Condition("grader-runs", False, f"{counts}: {_few(problems)}")
    return Condition("grader-runs", True, f"{counts}, each as declared")


def check_frozen(spec: WorldSpec) -> Condition:
    """The public tasks match their freeze file."""
    problems = freeze_problems(spec)
    if problems:
        return Condition("frozen", False, _few(problems), "maintainer")
    return Condition("frozen", True, f"{len(public_tasks(spec))} tasks match the freeze file")


def check_calibration(spec: WorldSpec, path: Path) -> Condition:
    """A calibration report of real models that covers every modifier of the world."""
    name, waits = "calibration", REAL_RUNS
    if not path.is_file():
        return Condition(name, False, f"no calibration report at {path}", waits)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return Condition(name, False, f"{path} is not JSON: {error}", waits)
    if not isinstance(report, dict) or report.get("format") != REPORT_FORMAT:
        return Condition(name, False, f"{path} is not a {REPORT_FORMAT} report", waits)
    problems: list[str] = []
    if report.get("version") != task_set_version(spec):
        problems.append(f"it is for {report.get('version')}, not {task_set_version(spec)}")
    models = [str(model) for model in report.get("models", [])]
    real = [model for model in models if not model.startswith(FAKE_PREFIX)]
    if len(real) < MIN_CALIBRATION_MODELS or len(real) != len(models):
        needed = f"{MIN_CALIBRATION_MODELS}+ real ones needed and no fake one"
        problems.append(f"{len(real)} real model(s) of {len(models)}; {needed}")
    covered = {row.get("name") for row in report.get("modifiers", []) if row.get("tasks")}
    if spec.name not in registry.names():
        return Condition(name, False, f"{spec.name} registers no stress modifiers")
    missing = sorted(set(registry.get(spec.name).names()) - covered)
    if missing:
        problems.append(f"no row for {', '.join(missing)}")
    if problems:
        return Condition(name, False, _few(problems), waits)
    best = report.get("best_stressed")
    under = "yes" if report.get("best_under_target") else "no"
    detail = f"{len(real)} models, {len(covered)} modifiers; best on the stressed set {best}%"
    return Condition(name, True, f"{detail} (under the target: {under})")


def result_dirs(spec: WorldSpec, root: Path) -> tuple[list[Path], list[Path]]:
    """Run directories under ``root`` of this world's public and of its hidden set."""
    public: list[Path] = []
    hidden: list[Path] = []
    version = task_set_version(spec)
    for manifest_path in sorted(root.rglob("manifest.json")):
        try:
            manifest = read_manifest(manifest_path.parent)
        except ValueError:
            public.append(manifest_path.parent)  # check() reports why it does not load
            continue
        if manifest is None or manifest.config.world != spec.name:
            continue
        if manifest.config.tasks == version + HIDDEN_SUFFIX:
            hidden.append(manifest_path.parent)
        elif manifest.config.tasks == version:
            public.append(manifest_path.parent)
    return public, hidden


@dataclass(slots=True)
class Results:
    """The result directories that pass, and the condition about them."""

    condition: Condition
    passing: list[RunDir]
    hidden: list[Path]


def check_results(
    spec: WorldSpec,
    root: Path | None,
    *,
    min_models: int = MIN_MODELS,
    min_runs: int = MIN_RUNS,
    allow_fake: bool = False,
) -> Results:
    """At least ``min_models`` models whose run directories pass every submission check."""
    name, waits = "results", REAL_RUNS
    if root is None or not root.is_dir():
        where = "no --results directory given" if root is None else f"{root} is not a directory"
        return Results(Condition(name, False, where, waits), [], [])
    public, hidden = result_dirs(spec, root)
    passing: list[RunDir] = []
    problems: list[str] = []
    for path in public:
        checked = check(path, allow_fake=allow_fake, min_runs=min_runs)
        if checked.ok and checked.run_dir is not None:
            passing.append(checked.run_dir)
        else:
            problems.append(f"{path.name}: {checked.problems[0]}")
    models = sorted({run_dir.manifest.config.model_slug for run_dir in passing})
    if len(models) != len(passing):
        problems.append("a model has more than one passing run directory; keep one")
    counts = f"{len(models)} of {min_models} models complete and reproduced"
    if len(models) < min_models or problems:
        detail = counts + (f"; {_few(problems)}" if problems else "")
        return Results(Condition(name, False, detail, waits), passing, hidden)
    runs = sum(len(run_dir.records) for run_dir in passing)
    detail = f"{counts}: {', '.join(models)} ({runs} runs, every one reproduced)"
    return Results(Condition(name, True, detail), passing, hidden)


def check_published(spec: WorldSpec, data_dir: Path, results: Results) -> Condition:
    """The committed ``<data_dir>/<world>/results.json`` is what the results export."""
    name, waits = "published", REAL_RUNS
    path = data_dir / spec.name / "results.json"
    if not path.is_file():
        return Condition(name, False, f"no leaderboard file at {path}", waits)
    if not results.condition.passed:
        return Condition(name, False, "the results do not pass yet, so nothing to compare", waits)
    problems: list[str] = []
    hidden_dirs: list[RunDir] = []
    for hidden_path in results.hidden:
        try:
            replay = verify(hidden_path)
        except HiddenSetError as error:
            problems.append(f"a hidden run directory cannot be checked: {error}")
            continue
        if not replay.ok or not replay.verdicts:
            bad = sum(not verdict.ok for verdict in replay.verdicts)
            problems.append(f"{bad} hidden run(s) in {hidden_path.name} do not reproduce")
            continue
        hidden_dirs.append(load_run_dir(hidden_path))
    if problems:
        return Condition(name, False, _few(problems), "maintainer")
    published = json.loads(path.read_text(encoding="utf-8"))
    built = build_world(spec, [*results.passing, *hidden_dirs], CHECK_DATE).results
    published.pop("updated", None)
    built.pop("updated", None)
    if published != built:
        mine = {model["id"] for model in built["models"]}
        theirs = {model.get("id") for model in published.get("models", [])}
        differ = f"models {sorted(mine ^ theirs)} differ" if mine != theirs else "numbers differ"
        detail = f"{path} is not what the results export ({differ}); run `sandbox export` again"
        return Condition(name, False, detail, "maintainer")
    hidden_note = f", hidden aggregates from {len(hidden_dirs)}" if hidden_dirs else ""
    return Condition(name, True, f"{path} matches the results{hidden_note}")


def tracked_files(repo: Path) -> list[Path]:
    """Every file git tracks in ``repo``, or would add: untracked files that are not ignored."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return [repo / name for name in out.split("\0") if name]


def check_no_hidden(repo: Path) -> Condition:
    """The two checks of ``just no-hidden``, plus a canary search, over every tracked file."""
    name = "no-hidden"
    script = repo / LEAK_SCRIPT
    if not (repo / ".git").exists() or not script.is_file():
        return Condition(name, False, f"{repo} is not a checkout of this repository", "maintainer")
    files = [path for path in tracked_files(repo) if path.is_file()]
    marked = subprocess.run(
        [sys.executable, str(script), *map(str, files)], capture_output=True, text=True
    )
    problems = [line for line in marked.stderr.splitlines() if line.strip()]
    specs = [worlds.get(world) for world in worlds.names()]
    problems += public_leaks(specs, [repo / part for part in SCANNED if (repo / part).exists()])
    problems += [f"{found.path}:{found.line}: a canary" for found in scan(files, {})]
    if marked.returncode not in (0, 1):
        problems.append(f"{LEAK_SCRIPT} exited with {marked.returncode}")
    if problems:
        return Condition(name, False, f"{len(problems)} finding(s): {_few(problems)}")
    return Condition(name, True, f"{len(files)} files git ships: no canary, no hidden task")


def release_check(
    task_set: str,
    *,
    repo: Path,
    results: Path | None,
    calibration: Path | None = None,
    published: Path | None = None,
    min_models: int = MIN_MODELS,
    min_runs: int = MIN_RUNS,
    allow_fake: bool = False,
) -> GateReport:
    """Check every release condition for ``task_set``; never stops at the first failure."""
    spec = world_of(task_set)
    calibration = calibration or repo / "docs" / "calibration" / task_set / "report.json"
    published = published or repo / "leaderboard" / "public" / "data"
    found = check_results(
        spec, results, min_models=min_models, min_runs=min_runs, allow_fake=allow_fake
    )
    conditions = [
        check_public_tasks(spec),
        check_hidden_set(spec),
        check_signoff(spec),
        check_grader_runs(spec),
        check_frozen(spec),
        check_calibration(spec, calibration),
        found.condition,
        check_published(spec, published, found),
        check_no_hidden(repo),
    ]
    return GateReport(task_set, conditions)


def report_lines(report: GateReport) -> list[str]:
    """The gate's output, one line per condition, then a verdict."""
    lines = [f"release-check {report.task_set}"]
    width = max(len(condition.name) for condition in report.conditions)
    for condition in report.conditions:
        mark = "PASS" if condition.passed else "FAIL"
        waits = "" if condition.passed else f"  [waits on: {condition.waits_on}]"
        lines.append(f"{mark} {condition.name:<{width}}  {condition.detail}{waits}")
    passed = sum(condition.passed for condition in report.conditions)
    verdict = "ready to release" if report.ok else f"failing: {', '.join(report.failing)}"
    lines += ["", f"{passed} of {len(report.conditions)} conditions pass; {verdict}"]
    return lines


__all__ = [
    "FREEZE_FORMAT",
    "MAX_TASKS",
    "MIN_MODELS",
    "MIN_RUNS",
    "MIN_TASKS",
    "Condition",
    "GateReport",
    "Results",
    "check_calibration",
    "check_frozen",
    "check_grader_runs",
    "check_hidden_set",
    "check_no_hidden",
    "check_public_tasks",
    "check_published",
    "check_results",
    "check_signoff",
    "freeze_file",
    "freeze_problems",
    "frozen_list",
    "release_check",
    "report_lines",
    "world_of",
    "write_freeze",
]
