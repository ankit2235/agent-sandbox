"""``sandbox run``: every (task, run) pair of a task set, resumable, in a run directory.

Layout of ``--out``::

    manifest.json              what was asked for, and how many runs finished or errored
    spans.jsonl                OpenTelemetry spans (see :mod:`runner.tracing`)
    runs/<run_id>/trace.jsonl  the full trace, agent messages included
    runs/<run_id>/result.json  the record: grade, usage, stop reason; written last

A run is finished when its ``result.json`` says ``completed``. Running the same
command again skips finished runs and redoes the rest: a run killed midway has no
``result.json`` and starts again from a fresh world, and an ``error`` run (the
provider kept failing) is retried. Resuming with different settings is refused.

Wall-clock time is used in exactly one place: the sleep between provider retries.
Inside an episode, time is the episode's :class:`kit.clock.SimClock`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from typing import Any, Literal

import anyio
from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict, Field

from kit.clock import EPOCH, SimClock
from kit.episode import BaseEpisode
from kit.rng import SeededRNG
from kit.tasks import load_tasks
from kit.types import GradeResult, TaskSpec, Usage, Visibility
from kit.world import WorldSpec, load_grader, worlds
from stress.core import stressed_set

from .agent import CallModel, Caps, run_agent, system_prompt
from .models import Message, ModelFactory, ModelReply, ProviderError, model_factory
from .tracing import TRACER_NAME, tracer_provider

RUN_FORMAT = "sandbox-run-v1"
MANIFEST = "manifest.json"
RUNS_DIR = "runs"
TRACE_FILE = "trace.jsonl"
RESULT_FILE = "result.json"
HIDDEN_SUFFIX = "-hidden"
STRESSED_SUFFIX = "-stressed"
STRESSED_SEED = 0
"""The public stressed set is always built with seed 0 (decision in DECISIONS.md)."""

BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0

Sleep = Callable[[float], None]


class RunConfig(BaseModel):
    """Everything that decides what a run directory contains. Resuming must match it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    world: str
    tasks: str
    """A task set (``payments-v0.1``, ``-stressed``, ``-hidden``) or a task directory."""
    model: str
    model_name: str
    provider: str
    open_weight: bool = False
    runs: int = Field(default=1, ge=1)
    sample: int | None = Field(default=None, ge=1)
    sample_seed: int = 0
    max_steps: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost: float | None = Field(default=None, gt=0)
    temperature: float | None = 0.0
    retries: int = Field(default=5, ge=0)
    prompt: str = ""
    """The system prompt's id, e.g. ``payments/v1``; filled in by :func:`execute`."""

    @property
    def model_slug(self) -> str:
        """The model id as a filename- and URL-safe slug: ``openai-gpt-4-1-mini``."""
        return slug(self.model)

    @property
    def caps(self) -> Caps:
        """The per-run caps."""
        return Caps(max_steps=self.max_steps, max_tokens=self.max_tokens, max_cost=self.max_cost)


class RunStatus(BaseModel):
    """How far a run directory has got."""

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    completed: int = 0
    errored: int = 0
    pending: int = 0


class Manifest(BaseModel):
    """``manifest.json``."""

    model_config = ConfigDict(extra="forbid")

    format: str = RUN_FORMAT
    config: RunConfig
    version: str
    """The world and task-set version every result carries, e.g. ``payments-v0.1``."""
    tasks: list[str]
    status: RunStatus = Field(default_factory=RunStatus)


class RunRecord(BaseModel):
    """``result.json``: one finished (or failed) run of one task."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    task_hash: str
    world: str
    version: str
    category: str
    difficulty: str
    visibility: Visibility
    attempt: int
    seed: int
    status: Literal["completed", "error"]
    stop_reason: str | None = None
    error: str | None = None
    grade: GradeResult | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_known: bool = True
    steps: int = 0
    """Tool calls applied to the world."""
    model_turns: int = 0
    malformed_calls: int = 0
    retries: int = 0

    @property
    def passed(self) -> bool:
        """Whether the run was graded and passed."""
        return self.grade is not None and self.grade.passed


class RunFailed(Exception):
    """A retriable provider error did not clear within ``--retries``."""


class ResumeMismatch(ValueError):
    """The run directory was started with different settings or different tasks."""


def slug(value: str) -> str:
    """Lowercase, with every run of other characters turned into one dash."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def task_hash(task: TaskSpec) -> str:
    """A fingerprint of a task, so a resumed or verified run knows the task did not change."""
    return hashlib.sha256(task.model_dump_json().encode()).hexdigest()


def task_set_version(spec: WorldSpec) -> str:
    """``<world>-<version>``, the label every result carries."""
    return f"{spec.name}-{spec.version}"


def resolve_task_set(spec: WorldSpec, value: str) -> list[TaskSpec]:
    """The tasks a ``--tasks`` value names: the public, stressed or hidden set, or a directory.

    Hidden tasks load only as the hidden set, from ``HIDDEN_TASKS_DIR``, and only if they
    match the hashes the world published (:mod:`runner.hidden`).
    """
    from .hidden import load_hidden

    base = task_set_version(spec)
    if value == base:
        tasks = spec.task_source().split(spec.version)
    elif value == base + STRESSED_SUFFIX:
        tasks = list(_stressed_set(spec))
    elif value == base + HIDDEN_SUFFIX:
        tasks = load_hidden(spec)
    elif Path(value).is_dir():
        tasks = load_tasks(value)
    else:
        msg = (
            f"unknown task set {value!r}; use {base}, {base}{STRESSED_SUFFIX}, "
            f"{base}{HIDDEN_SUFFIX} or a task directory"
        )
        raise ValueError(msg)
    for task in tasks:
        if task.world != spec.name or task.version != spec.version:
            msg = f"task {task.id} is {task.world}/{task.version}, not {spec.name}/{spec.version}"
            raise ValueError(msg)
        if task.visibility == "hidden" and value != base + HIDDEN_SUFFIX:
            msg = f"task {task.id} is hidden: hidden tasks load only as {base}{HIDDEN_SUFFIX}"
            raise ValueError(msg)
    if not tasks:
        msg = f"task set {value!r} is empty"
        raise ValueError(msg)
    return tasks


_STRESSED: dict[str, tuple[TaskSpec, ...]] = {}


def _stressed_set(spec: WorldSpec) -> tuple[TaskSpec, ...]:
    """The public stressed set, built once per process: it is pure, and slow to build."""
    key = task_set_version(spec)
    if key not in _STRESSED:
        public = spec.task_source().split(spec.version)
        _STRESSED[key] = tuple(stressed_set(public, STRESSED_SEED, runs_dir=spec.runs_dir))
    return _STRESSED[key]


def select_tasks(spec: WorldSpec, config: RunConfig) -> list[TaskSpec]:
    """The task set, sampled with a seed when ``--sample`` asks for fewer tasks."""
    tasks = resolve_task_set(spec, config.tasks)
    if config.sample is not None and config.sample < len(tasks):
        tasks = SeededRNG(config.sample_seed).child("sample").sample(tasks, config.sample)
    return sorted(tasks, key=lambda task: task.id)


def run_id_for(config: RunConfig, task: TaskSpec, attempt: int) -> str:
    """``<model-slug>-<task-id>-r<n>``."""
    return f"{config.model_slug}-{task.id}-r{attempt}"


def backoff(retry: int) -> float:
    """Seconds to wait before retry ``retry`` (0-based): 2, 4, 8, ... capped at 60."""
    return float(min(BACKOFF_BASE * 2**retry, BACKOFF_MAX))


class RetryingModel:
    """A model call that retries retriable provider errors with exponential backoff."""

    def __init__(self, call: CallModel, retries: int, sleep: Sleep) -> None:
        self._call = call
        self._retries = retries
        self._sleep = sleep
        self.retried = 0

    def __call__(self, messages: Sequence[Message], tools: Sequence[Message]) -> ModelReply:
        """Call the model; sleep and try again on a retriable error."""
        for retry in range(self._retries + 1):
            try:
                return self._call(messages, tools)
            except ProviderError as error:
                if not error.retriable:
                    raise
                if retry == self._retries:
                    msg = f"provider still failing after {self._retries} retries: {error}"
                    raise RunFailed(msg) from error
                self.retried += 1
                self._sleep(backoff(retry))
        msg = "unreachable: the loop returns or raises"
        raise AssertionError(msg)


# ------------------------------------------------------------------ the store
def read_manifest(out: Path) -> Manifest | None:
    """The run directory's manifest, if it has one."""
    path = out / MANIFEST
    if not path.exists():
        return None
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically: a half-written file never looks finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def load_records(out: Path) -> list[RunRecord]:
    """Every run record in a run directory, sorted by run id."""
    worlds.discover()  # records name failure types, which worlds register
    paths = sorted((out / RUNS_DIR).glob(f"*/{RESULT_FILE}"))
    return [RunRecord.model_validate_json(path.read_text(encoding="utf-8")) for path in paths]


def run_status(total: int, records: Sequence[RunRecord]) -> RunStatus:
    """Counts for the manifest and the summary."""
    completed = sum(record.status == "completed" for record in records)
    errored = sum(record.status == "error" for record in records)
    return RunStatus(
        total=total, completed=completed, errored=errored, pending=total - completed - errored
    )


# ------------------------------------------------------------------- running
def execute(
    config: RunConfig,
    out: Path,
    *,
    concurrency: int = 1,
    factory: ModelFactory | None = None,
    sleep: Sleep = time.sleep,  # purity-allow: the backoff between provider retries only
    env: dict[str, str] | None = None,
    report: Callable[[RunRecord], None] | None = None,
) -> Manifest:
    """Run every unfinished (task, run) pair and return the updated manifest."""
    spec = worlds.get(config.world)
    prompt_id, prompt = system_prompt(spec.name)
    config = config.model_copy(update={"prompt": prompt_id})
    tasks = select_tasks(spec, config)
    manifest = _start(out, config, spec, tasks)
    done = {record.run_id: record for record in load_records(out)}
    pending: list[tuple[TaskSpec, int]] = []
    for task in tasks:
        for attempt in range(1, config.runs + 1):
            record = done.get(run_id_for(config, task, attempt))
            if record is not None and record.task_hash != task_hash(task):
                msg = f"task {task.id} changed since {record.run_id} ran; start a new run directory"
                raise ResumeMismatch(msg)
            if record is None or record.status != "completed":
                pending.append((task, attempt))
    build = factory or model_factory(config.model, temperature=config.temperature)
    provider = tracer_provider(out, env)
    tracer = provider.get_tracer(TRACER_NAME)
    one = partial(_run_one, spec, config, prompt, build, tracer, sleep, out)
    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    try:
        futures: list[Future[RunRecord]] = [pool.submit(one, task, n) for task, n in pending]
        total = len(tasks) * config.runs
        for future in as_completed(futures):
            record = future.result()
            # Kept current after every run, so it is right even if the process is killed.
            manifest.status = run_status(total, load_records(out))
            write_json(out / MANIFEST, manifest.model_dump(mode="json"))
            if report is not None:
                report(record)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
        provider.shutdown()
        manifest.status = run_status(len(tasks) * config.runs, load_records(out))
        write_json(out / MANIFEST, manifest.model_dump(mode="json"))
    return manifest


def _start(out: Path, config: RunConfig, spec: WorldSpec, tasks: list[TaskSpec]) -> Manifest:
    ids = [task.id for task in tasks]
    existing = read_manifest(out)
    if existing is not None:
        if existing.config != config:
            before = existing.config.model_dump()
            after = config.model_dump()
            changed = sorted(key for key in after if before.get(key) != after[key])
            msg = f"{out} was started with different settings ({', '.join(changed)})"
            raise ResumeMismatch(msg)
        if existing.tasks != ids:
            msg = f"{out} was started with a different task list"
            raise ResumeMismatch(msg)
        return existing
    manifest = Manifest(config=config, version=task_set_version(spec), tasks=ids)
    manifest.status = run_status(len(ids) * config.runs, [])
    write_json(out / MANIFEST, manifest.model_dump(mode="json"))
    return manifest


def _run_one(
    spec: WorldSpec,
    config: RunConfig,
    prompt: str,
    build: ModelFactory,
    tracer: Tracer,
    sleep: Sleep,
    out: Path,
    task: TaskSpec,
    attempt: int,
) -> RunRecord:
    """One run: a fresh world, the agent loop, the grade, the trace and the record."""
    run_id = run_id_for(config, task, attempt)
    directory = out / RUNS_DIR / run_id
    episode = spec.make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
    if not isinstance(episode, BaseEpisode):
        msg = f"world {spec.name!r} must build its episodes on kit.episode.BaseEpisode"
        raise TypeError(msg)
    model = build(spec, task)
    call = RetryingModel(model.complete, config.retries, sleep)
    record = RunRecord(
        run_id=run_id,
        task_id=task.id,
        task_hash=task_hash(task),
        world=spec.name,
        version=task_set_version(spec),
        category=task.category,
        difficulty=task.difficulty,
        visibility=task.visibility,
        attempt=attempt,
        seed=task.seed,
        status="error",
    )
    attributes = {
        "sandbox.world": spec.name,
        "sandbox.task_id": task.id,
        "sandbox.run_id": run_id,
        "gen_ai.request.model": config.model,
    }
    with tracer.start_as_current_span("sandbox.run", attributes=attributes) as span:
        try:
            first = episode.reset(task)
            agent = partial(
                run_agent,
                episode,
                task,
                first,
                call,
                prompt=prompt,
                caps=config.caps,
                tracer=tracer,
            )
            try:
                result = anyio.run(agent)
            except RunFailed as error:
                record.error = str(error)
            else:
                grader = load_grader(task.grader)
                record.grade = grader(episode.state, episode.trace.grader_view(), task)
                record.status = "completed"
                record.stop_reason = result.stop_reason
                record.usage = result.usage
                record.cost_known = result.cost_known
                record.steps = result.tool_calls
                record.model_turns = result.model_turns
                record.malformed_calls = result.malformed_calls
            record.retries = call.retried
            trace = episode.trace.model_copy(update={"run_id": run_id})
            trace.write_jsonl(directory / TRACE_FILE)
        finally:
            episode.close()
        span.set_attribute("sandbox.status", record.status)
        if record.grade is not None:
            span.set_attribute("sandbox.passed", record.grade.passed)
            span.set_attribute("sandbox.score", record.grade.score)
            span.set_attribute("sandbox.stop_reason", str(record.stop_reason))
        span.set_attribute("sandbox.cost_usd", record.usage.cost_usd)
    write_json(directory / RESULT_FILE, record.model_dump(mode="json"))
    return record


__all__ = [
    "MANIFEST",
    "RESULT_FILE",
    "TRACE_FILE",
    "Manifest",
    "ResumeMismatch",
    "RetryingModel",
    "RunConfig",
    "RunFailed",
    "RunRecord",
    "RunStatus",
    "backoff",
    "execute",
    "load_records",
    "read_manifest",
    "resolve_task_set",
    "run_id_for",
    "select_tasks",
    "slug",
    "task_hash",
    "task_set_version",
]
