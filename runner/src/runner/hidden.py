"""The hidden task set: generate it into the private repository, publish its hashes, check it.

The private repository is found through ``HIDDEN_TASKS_DIR`` and holds, per world::

    <world>/<version>/<category>/<id>.yaml   hidden tasks (visibility hidden, one canary each)
    <world>/<version>/set.json               the secret seed and what each task was made from
    <world>/runs-<version>/<id>.yaml         each task's golden run and its trap runs

The public repository holds only ``<tasks_dir>/hidden-<version>.json``: the set's
name, its size and the sha256 of every task (:func:`content_hash`), so
anyone can check later that the set did not change. Never ids, never content.

A canary is a tag ``sandbox-canary:<uuid>``, drawn from ``SeededRNG(seed)`` per task.
Tags are never shown to the agent and no trace or record holds them, so a canary in a
model's output means the model has seen the hidden task itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kit.rng import SeededRNG
from kit.scripted import Run, RunFile, load_runs
from kit.tasks import hidden_tasks_dir, load_task, load_tasks
from kit.types import TaskSpec
from kit.world import WorldSpec, load_grader
from stress.core import NotApplicable, applicable, apply, scripted_runs

from .runs import task_set_version
from .validate import play_runs, played

CANARY_PREFIX = "sandbox-canary:"
CANARY_RE = re.compile(
    re.escape(CANARY_PREFIX)
    + r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
SET_FILE = "set.json"
HASH_FORMAT = "hidden-hashes-v1"
MAX_MODIFIERS = 2
PASSES = 3


class HiddenSetError(ValueError):
    """The hidden set is missing, malformed, or not the one whose hashes were published."""


# ------------------------------------------------------------------- layout
def private_root() -> Path:
    """``HIDDEN_TASKS_DIR``, which must be set."""
    root = hidden_tasks_dir()
    if root is None:
        msg = "HIDDEN_TASKS_DIR is not set; point it at the private task repository"
        raise HiddenSetError(msg)
    return root


def tasks_dir(spec: WorldSpec, root: Path) -> Path:
    """Where a world's hidden tasks live in the private repository."""
    return root / spec.name / spec.version


def runs_dir(spec: WorldSpec, root: Path) -> Path:
    """Where their golden and trap runs live, outside the task folder."""
    return root / spec.name / f"runs-{spec.version}"


def hash_file(spec: WorldSpec) -> Path:
    """The published hash list, beside the world's public tasks."""
    if spec.tasks_dir is None:
        msg = f"world {spec.name!r} ships no tasks directory to publish hashes in"
        raise HiddenSetError(msg)
    return spec.tasks_dir / f"hidden-{spec.version}.json"


def set_name(spec: WorldSpec) -> str:
    """``<world>-<version>-hidden``."""
    return f"{task_set_version(spec)}-hidden"


def canary(seed: int, task_id: str) -> str:
    """The canary for one hidden task: seeded, never from unseeded randomness."""
    return f"{CANARY_PREFIX}{SeededRNG(seed).child(f'canary/{task_id}').uuid()}"


def canaries_of(task: TaskSpec) -> list[str]:
    """The task's canary tags."""
    return [tag for tag in task.tags if tag.startswith(CANARY_PREFIX)]


# --------------------------------------------------------------- generation
@dataclass(slots=True)
class Generated:
    """What :func:`generate` wrote."""

    tasks: list[TaskSpec] = field(default_factory=list)
    skipped: int = 0
    """Candidates dropped because a run did not behave as its file says."""


def _repo_root(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _refuse_public(destination: Path) -> None:
    """Refuse to write into the repository this code runs from."""
    here = _repo_root(Path(__file__).resolve())
    there = _repo_root(destination.resolve())
    if here is not None and there == here:
        msg = f"{destination} is inside the public repository {here}; hidden tasks never go there"
        raise HiddenSetError(msg)


def _behaves(spec: WorldSpec, task: TaskSpec, runs: RunFile) -> RunFile | None:
    """The runs that play as declared; None if the golden run fails or no trap is left."""
    golden = played(spec, task, runs.runs["golden"])
    if golden is None or not golden.passed:
        return None
    kept: dict[str, Run] = {"golden": runs.runs["golden"]}
    for name in runs.wrong:
        trap = runs.runs[name]
        result = played(spec, task, trap)
        grade = result.grade if result is not None else None
        if grade is not None and grade.failure_type == trap.fails_with:
            kept[name] = trap
    if len(kept) == 1:
        return None
    return RunFile(task=runs.task, runs=kept)


def _candidate(
    spec: WorldSpec,
    base: TaskSpec,
    pick: tuple[str, ...],
    task_seed: int,
    hidden_id: str,
    seed: int,
) -> tuple[TaskSpec, RunFile, int] | None:
    """One hidden task and its runs, if the stressed task and its runs behave."""
    assert spec.runs_dir is not None
    try:
        stressed = apply(base, list(pick), task_seed)
        runs = scripted_runs(stressed, load_runs(base.id, spec.runs_dir), task_id=hidden_id)
    except NotApplicable:
        return None
    task = stressed.model_copy(
        update={
            "id": hidden_id,
            "visibility": "hidden",
            "tags": [*stressed.tags, "stress", canary(seed, hidden_id)],
            "reviewer_signoff": None,
        }
    )
    kept = _behaves(spec, task, runs)
    return None if kept is None else (task, kept, task_seed)


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def generate(spec: WorldSpec, count: int, seed: int, root: Path) -> Generated:
    """Write ``count`` hidden tasks, with runs, made from the public tasks and ``seed``.

    Public tasks are taken in a seeded order, in up to ``PASSES`` passes. A candidate
    gets one or two of the modifiers that apply to its base task with a trap run
    (seeded), each base and pick at most once, with a seed derived from ``seed``; it is
    kept only if its golden run passes and at least one trap run fails with the
    failure type it declares. ``seed`` is the set's secret: whoever knows it can
    rebuild the set from the public code.
    """
    if count < 1:
        msg = "count must be at least 1"
        raise HiddenSetError(msg)
    _refuse_public(root)
    folder = tasks_dir(spec, root)
    if folder.exists() and any(folder.rglob("*.y*ml")):
        msg = f"{folder} already holds a hidden set; retire it before generating a new one"
        raise HiddenSetError(msg)
    if spec.runs_dir is None:
        msg = f"world {spec.name!r} ships no run files to build hidden runs from"
        raise HiddenSetError(msg)
    rng = SeededRNG(seed).child(f"hidden/{spec.name}/{spec.version}")
    pool = spec.task_source().split(spec.version)
    rng.shuffle(pool)
    options = {
        base.id: sorted(applicable(base, rng.randint(0, 2**31), runs_dir=spec.runs_dir))
        for base in pool
    }
    out = Generated()
    made: dict[str, dict[str, Any]] = {}
    kept_runs: dict[str, RunFile] = {}
    tried: set[tuple[str, tuple[str, ...]]] = set()
    for _ in range(PASSES):
        for base in pool:
            if len(out.tasks) == count:
                break
            names = options[base.id]
            if not names:
                continue
            pick = tuple(rng.sample(names, min(len(names), rng.randint(1, MAX_MODIFIERS))))
            if (base.id, pick) in tried:
                continue
            tried.add((base.id, pick))
            hidden_id = f"hidden-{len(out.tasks) + 1:03d}"
            found = _candidate(spec, base, pick, rng.randint(0, 2**31), hidden_id, seed)
            if found is None:
                out.skipped += 1
                continue
            task, kept, task_seed = found
            out.tasks.append(task)
            kept_runs[hidden_id] = kept
            made[hidden_id] = {"base": base.id, "modifiers": list(task.stress), "seed": task_seed}
    if len(out.tasks) < count:
        msg = f"only {len(out.tasks)} of {count} hidden tasks could be made for {spec.name}"
        raise HiddenSetError(msg)
    for task in out.tasks:
        _dump(folder / task.category / f"{task.id}.yaml", task.model_dump(mode="json"))
        _dump(
            runs_dir(spec, root) / f"{task.id}.yaml",
            kept_runs[task.id].model_dump(mode="json", exclude_none=True),
        )
    (folder / SET_FILE).write_text(
        json.dumps(
            {"set": set_name(spec), "seed": seed, "count": count, "tasks": made},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


# ------------------------------------------------------------------- hashes
def content_hash(task: TaskSpec) -> str:
    """sha256 of the task as canonical JSON (sorted keys): the same however the file is laid out."""
    canonical = json.dumps(task.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def set_hash(hashes: Iterable[str]) -> str:
    """One hash over the sorted task hashes."""
    return hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()


def hash_list(spec: WorldSpec, tasks: Sequence[TaskSpec]) -> dict[str, Any]:
    """The public hash list for a hidden set."""
    hashes = sorted(content_hash(task) for task in tasks)
    return {
        "format": HASH_FORMAT,
        "set": set_name(spec),
        "algorithm": "sha256",
        "count": len(hashes),
        "set_hash": set_hash(hashes),
        "tasks": hashes,
    }


def write_hashes(spec: WorldSpec, root: Path) -> Path:
    """Hash the private set and write the public list beside the world's tasks."""
    tasks = load_tasks(tasks_dir(spec, root))
    check_canaries(tasks)
    path = hash_file(spec)
    path.write_text(json.dumps(hash_list(spec, tasks), indent=2) + "\n", encoding="utf-8")
    return path


def published(spec: WorldSpec) -> dict[str, Any]:
    """The published hash list; a hidden set cannot be used until there is one."""
    path = hash_file(spec)
    if not path.exists():
        msg = f"no published hash list for {set_name(spec)} at {path}; run `sandbox hidden hashes`"
        raise HiddenSetError(msg)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != HASH_FORMAT or data.get("set") != set_name(spec):
        msg = f"{path} is not a {HASH_FORMAT} list for {set_name(spec)}"
        raise HiddenSetError(msg)
    if set_hash(data["tasks"]) != data["set_hash"] or len(data["tasks"]) != data["count"]:
        msg = f"{path} is inconsistent: its set hash or count does not match its task hashes"
        raise HiddenSetError(msg)
    return data


# ------------------------------------------------------------------- checks
def check_canaries(tasks: Sequence[TaskSpec]) -> None:
    """Every task is hidden and carries exactly one well-formed canary, unique in the set."""
    seen: dict[str, str] = {}
    for task in tasks:
        if task.visibility != "hidden":
            msg = f"{task.id} is in the hidden set but is not marked hidden"
            raise HiddenSetError(msg)
        found = canaries_of(task)
        if len(found) != 1 or not CANARY_RE.fullmatch(found[0]):
            msg = f"{task.id} must carry exactly one canary tag {CANARY_PREFIX}<uuid4>"
            raise HiddenSetError(msg)
        if found[0] in seen:
            msg = f"{task.id} repeats the canary of {seen[found[0]]}"
            raise HiddenSetError(msg)
        seen[found[0]] = task.id


def load_hidden(spec: WorldSpec) -> list[TaskSpec]:
    """The hidden set, from the private repository only, checked against its published hashes."""
    folder = tasks_dir(spec, private_root())
    if not folder.is_dir():
        msg = f"no hidden set for {spec.name} {spec.version} at {folder}"
        raise HiddenSetError(msg)
    tasks = load_tasks(folder)
    check_canaries(tasks)
    expected = published(spec)
    got = sorted(content_hash(task) for task in tasks)
    if got != expected["tasks"]:
        changed = len(set(got) ^ set(expected["tasks"]))
        msg = (
            f"the hidden set at {folder} does not match its published hashes "
            f"({len(got)} tasks here, {expected['count']} published, {changed} differing)"
        )
        raise HiddenSetError(msg)
    return tasks


@dataclass(slots=True)
class CheckReport:
    """What ``sandbox hidden check`` found: problems, and how many runs played."""

    tasks: int = 0
    runs: int = 0
    problems: list[str] = field(default_factory=list)


def check_set(spec: WorldSpec) -> CheckReport:
    """Everything about the hidden set: files, canaries, hashes, and every run plays as declared."""
    root = private_root()
    tasks = load_hidden(spec)
    report = CheckReport(tasks=len(tasks))
    schema = None
    if spec.tasks_dir is not None and (spec.tasks_dir / "schema.json").exists():
        schema = json.loads((spec.tasks_dir / "schema.json").read_text(encoding="utf-8"))
    for path in sorted(tasks_dir(spec, root).rglob("*.y*ml")):
        task = load_task(path)
        if path.stem != task.id or path.parent.name != task.category:
            report.problems.append(f"{path}: must be <category>/<id>.yaml")
        spec.validate_task(task)
        load_grader(task.grader)
        if schema is not None:
            import jsonschema

            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            jsonschema.validate(json.loads(json.dumps(raw, default=str)), schema)
    for task in tasks:
        try:
            runs = load_runs(task.id, runs_dir(spec, root))
        except FileNotFoundError:
            report.problems.append(f"{task.id}: no run file")
            continue
        outcome = play_runs(spec, task, runs)
        report.runs += outcome.runs
        report.problems += outcome.problems
    return report


def public_leaks(specs: Iterable[WorldSpec], paths: Iterable[Path]) -> list[str]:
    """Task files under ``paths`` whose hash is one of a published hidden set's."""
    known: dict[str, str] = {}
    for spec in specs:
        if spec.tasks_dir is not None and hash_file(spec).exists():
            known.update(dict.fromkeys(published(spec)["tasks"], set_name(spec)))
    leaks: list[str] = []
    for root in paths:
        files = [root] if root.is_file() else sorted(root.rglob("*.y*ml"))
        for path in files:
            try:
                task = load_task(path)
            except (ValueError, yaml.YAMLError, UnicodeDecodeError):
                continue
            if content_hash(task) in known:
                leaks.append(f"{path}: a task of {known[content_hash(task)]}")
    return leaks


# --------------------------------------------------------------------- scan
@dataclass(frozen=True, slots=True)
class Sighting:
    """A canary found in some text."""

    path: Path
    line: int
    canary: str
    hidden_set: str | None
    """The set it belongs to, when the private repository is at hand."""


def known_canaries(specs: Iterable[WorldSpec], root: Path | None) -> dict[str, str]:
    """Every canary of every hidden set in the private repository, mapped to its set."""
    if root is None:
        return {}
    found: dict[str, str] = {}
    for spec in specs:
        folder = tasks_dir(spec, root)
        if folder.is_dir():
            for task in load_tasks(folder):
                found.update(dict.fromkeys(canaries_of(task), set_name(spec)))
    return found


def scan(paths: Iterable[Path], known: dict[str, str]) -> list[Sighting]:
    """Search files, or every file under directories (run directories included), for canaries.

    Any well-formed canary counts, known or not: the set it came from may have been
    rotated. A run directory holds nothing that carries a canary except what the model
    wrote, because tags never reach the agent, the trace or the record.
    """
    sightings: list[Sighting] = []
    for root in paths:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for match in CANARY_RE.finditer(line):
                    found = match.group(0)
                    sightings.append(Sighting(path, number, found, known.get(found)))
    return sightings


__all__ = [
    "CANARY_PREFIX",
    "CANARY_RE",
    "CheckReport",
    "Generated",
    "HiddenSetError",
    "Sighting",
    "canary",
    "check_canaries",
    "check_set",
    "content_hash",
    "generate",
    "hash_file",
    "hash_list",
    "known_canaries",
    "load_hidden",
    "private_root",
    "public_leaks",
    "published",
    "runs_dir",
    "scan",
    "set_hash",
    "set_name",
    "tasks_dir",
    "write_hashes",
]
