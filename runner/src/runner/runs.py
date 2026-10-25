"""``sandbox run``: every (task, run) pair of a task set, resumable, in a run directory.

Layout of ``--out``::

    manifest.json              what was asked for, and how many runs finished or errored
    spans.jsonl                OpenTelemetry spans (see :mod:`runner.tracing`)
    runs/<run_id>/trace.jsonl  the full trace, agent messages included
    runs/<run_id>/result.json  the record: grade, usage, stop reason; written last
    runs/<run_id>/decisions.jsonl  with --guard: the run's hash-chained decision records
    runs/<run_id>/episode/     with --isolation container, while the run is going: the
                               container's only writable path, removed when it ends

A run is finished when its ``result.json`` says ``completed``. Running the same
command again skips finished runs and redoes the rest: a run killed midway has no
``result.json`` and starts again from a fresh world, and an ``error`` run (the
provider kept failing) is retried. Resuming with different settings is refused.

With ``--isolation container`` each run's world lives in its own locked-down container
(:mod:`runner.isolation`); the run directory comes out the same, byte for byte, so the
setting is not part of :class:`RunConfig` and a directory may be resumed either way.

A multi-agent task (:mod:`kit.roles`) is run with its counterparts (``--counterpart``,
:mod:`runner.counterpart`): the world's scripted players by default, or a model. Their calls
are in the same trace; the record's ``counterpart`` says who played them and what they used.
Multi-agent tasks do not run in containers yet (DECISIONS 271).

Wall-clock time is used in exactly one place here: the sleep between provider retries
(a guard's decision latency is measured in :mod:`guard.engine`, and never decides
anything). Inside an episode, time is the episode's :class:`kit.clock.SimClock`.
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

from .agent import AgentResult, CallModel, Caps, LocalEpisode, run_agent, system_prompt
from .counterpart import SCRIPTED, Counterparts, CounterpartSummary
from .guarding import GuardRunConfig, GuardSetup, GuardSummary
from .isolation import (
    CONTAINER_ERRORS,
    GUARD_REFUSAL,
    MULTI_AGENT_REFUSAL,
    ContainerEpisode,
    EpisodeFailed,
    Isolation,
    IsolationError,
    preflight,
)
from .models import (
    FAKE_PREFIX,
    Message,
    ModelFactory,
    ModelReply,
    ProviderError,
    model_factory,
)
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
    guard: GuardRunConfig | None = None
    """The guard in front of the world's high-impact tools (``--guard``), if any."""
    counterpart: str = SCRIPTED
    """Who plays the other agents of a multi-agent task: ``scripted`` or a model id."""

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
    guard: GuardSummary | None = None
    """With a guard: its decision counts and the decision log's head."""
    counterpart: CounterpartSummary | None = None
    """In a multi-agent task: who played the other agents, and what they did and used."""

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
    """A fingerprint of a task, so a resumed or verified run knows the task did not change.

    ``reviewer_signoff`` is left out: the agent never sees it, so a run made before a
    reviewer signed the task off still reproduces afterwards.
    """
    payload = task.model_dump_json(exclude={"reviewer_signoff"})
    return hashlib.sha256(payload.encode()).hexdigest()


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
    extra = extra_set(spec, value)
    if value == base:
        tasks = spec.task_source().split(spec.version)
    elif extra is not None:
        tasks = load_tasks(spec.task_sets[extra])
    elif value == base + STRESSED_SUFFIX:
        tasks = list(_stressed_set(spec))
    elif value == base + HIDDEN_SUFFIX:
        tasks = load_hidden(spec)
    elif Path(value).is_dir():
        tasks = load_tasks(value)
    else:
        extras = "".join(f", {base}-{name}" for name in spec.task_sets)
        msg = (
            f"unknown task set {value!r}; use {base}, {base}{STRESSED_SUFFIX}, "
            f"{base}{HIDDEN_SUFFIX}{extras} or a task directory"
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


def extra_set(spec: WorldSpec, value: str) -> str | None:
    """The name of the extra task set (``WorldSpec.task_sets``) ``value`` names, if it does."""
    base = task_set_version(spec)
    for name in spec.task_sets:
        if value == f"{base}-{name}":
            return name
    return None


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


def manifest_json(manifest: Manifest) -> dict[str, Any]:
    """``manifest.json``'s content.

    ``counterpart`` is written only when a model played it, so a single-agent run directory
    is byte for byte what it was before counterparts existed.
    """
    payload: dict[str, Any] = manifest.model_dump(mode="json")
    if manifest.config.counterpart == SCRIPTED:
        del payload["config"]["counterpart"]
    return payload


def record_json(record: RunRecord) -> dict[str, Any]:
    """``result.json``'s content; ``counterpart`` only in a multi-agent run."""
    payload: dict[str, Any] = record.model_dump(mode="json")
    if record.counterpart is None:
        del payload["counterpart"]
    return payload


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
    guard: GuardSetup | None = None,
    isolation: Isolation | None = None,
    counterpart_factory: ModelFactory | None = None,
) -> Manifest:
    """Run every unfinished (task, run) pair and return the updated manifest.

    With ``isolation``, every run's world runs in a locked-down container; Docker and the
    image are checked first (:class:`runner.isolation.IsolationError`). A multi-agent task's
    counterparts are the scripted ones, or ``config.counterpart``'s model, built with
    ``counterpart_factory`` when one is given (tests).
    """
    if (guard.config if guard is not None else None) != config.guard:
        msg = "the run's guard settings and the guard given to execute() differ"
        raise ValueError(msg)
    if isolation is not None:
        if guard is not None:
            raise IsolationError(GUARD_REFUSAL)
        preflight(isolation)
    spec = worlds.get(config.world)
    prompt_id, prompt = system_prompt(spec.name)
    config = config.model_copy(update={"prompt": prompt_id})
    tasks = select_tasks(spec, config)
    multi = [task.id for task in tasks if spec.is_multi_agent(task)]
    if isolation is not None and multi:
        raise IsolationError(MULTI_AGENT_REFUSAL)
    others = _counterpart_factory(config, counterpart_factory) if multi else None
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
    one = partial(
        _run_one, spec, config, prompt, build, tracer, sleep, out, guard, isolation, others
    )
    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    try:
        futures: list[Future[RunRecord]] = [pool.submit(one, task, n) for task, n in pending]
        total = len(tasks) * config.runs
        for future in as_completed(futures):
            record = future.result()
            # Kept current after every run, so it is right even if the process is killed.
            manifest.status = run_status(total, load_records(out))
            write_json(out / MANIFEST, manifest_json(manifest))
            if report is not None:
                report(record)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
        provider.shutdown()
        manifest.status = run_status(len(tasks) * config.runs, load_records(out))
        write_json(out / MANIFEST, manifest_json(manifest))
    return manifest


def _counterpart_factory(config: RunConfig, given: ModelFactory | None) -> ModelFactory | None:
    """The model factory for model counterparts; ``None`` for the scripted ones."""
    if config.counterpart == SCRIPTED:
        return None
    if given is not None:
        return given
    if config.counterpart.startswith(FAKE_PREFIX):
        msg = (
            f"--counterpart {config.counterpart}: use scripted (the fake counterpart) or a model id"
        )
        raise ValueError(msg)
    return model_factory(config.counterpart, temperature=config.temperature)


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
    write_json(out / MANIFEST, manifest_json(manifest))
    return manifest


def _run_one(
    spec: WorldSpec,
    config: RunConfig,
    prompt: str,
    build: ModelFactory,
    tracer: Tracer,
    sleep: Sleep,
    out: Path,
    guard: GuardSetup | None,
    isolation: Isolation | None,
    others: ModelFactory | None,
    task: TaskSpec,
    attempt: int,
) -> RunRecord:
    """One run: a fresh world, the agent loop, the grade, the trace and the record."""
    run_id = run_id_for(config, task, attempt)
    directory = out / RUNS_DIR / run_id
    model = build(spec, task)
    call = RetryingModel(model.complete, config.retries, sleep)
    other: RetryingModel | None = None
    if others is not None and spec.is_multi_agent(task):
        other = RetryingModel(others(spec, task).complete, config.retries, sleep)
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
        if isolation is None:
            players = Players(config.counterpart, other)
            _in_process(
                spec, task, prompt, config.caps, tracer, guard, call, record, directory, players
            )
        else:
            _in_container(task, prompt, config.caps, tracer, isolation, call, record, directory)
        span.set_attribute("sandbox.status", record.status)
        if record.grade is not None:
            span.set_attribute("sandbox.passed", record.grade.passed)
            span.set_attribute("sandbox.score", record.grade.score)
            span.set_attribute("sandbox.stop_reason", str(record.stop_reason))
        span.set_attribute("sandbox.cost_usd", record.usage.cost_usd)
    write_json(directory / RESULT_FILE, record_json(record))
    return record


def _completed(record: RunRecord, grade: GradeResult, result: AgentResult) -> None:
    record.grade = grade
    record.status = "completed"
    record.stop_reason = result.stop_reason
    record.usage = result.usage
    record.cost_known = result.cost_known
    record.steps = result.tool_calls
    record.model_turns = result.model_turns
    record.malformed_calls = result.malformed_calls


class Players:
    """Who plays a run's counterparts: the scripted players, or a model's calls."""

    def __init__(self, player: str, call: RetryingModel | None) -> None:
        self.player = player
        self.call = call


def _in_process(
    spec: WorldSpec,
    task: TaskSpec,
    prompt: str,
    caps: Caps,
    tracer: Tracer,
    guard: GuardSetup | None,
    call: RetryingModel,
    record: RunRecord,
    directory: Path,
    players: Players | None = None,
) -> None:
    """The world in this process, reached over the in-memory MCP transport."""
    episode = spec.make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
    if not isinstance(episode, BaseEpisode):
        msg = f"world {spec.name!r} must build its episodes on kit.episode.BaseEpisode"
        raise TypeError(msg)
    try:
        first = episode.reset(task)
        run_guard = guard.for_run(task, episode, directory) if guard is not None else None
        counterparts: Counterparts | None = None
        listed: TaskSpec | None = None
        if spec.is_multi_agent(task):
            players = players or Players(SCRIPTED, None)
            counterparts = Counterparts(
                spec, task, episode, players.player, caps=caps, call_model=players.call
            )
            listed = task.model_copy(update={"allowed_tools": spec.agent_tools(task)})
        agent = partial(
            run_agent,
            LocalEpisode(episode, task, listed),
            task,
            first.text,
            call,
            prompt=prompt,
            caps=caps,
            tracer=tracer,
            guard=run_guard.check if run_guard is not None else None,
            counterparts=counterparts,
        )
        try:
            result = anyio.run(agent)
        except RunFailed as error:
            record.error = str(error)
        else:
            grader = load_grader(task.grader)
            _completed(record, grader(episode.state, episode.trace.grader_view(), task), result)
        record.retries = call.retried
        if counterparts is not None:
            record.counterpart = counterparts.summary
            if players is not None and players.call is not None:
                record.retries += players.call.retried
        if run_guard is not None:
            record.guard = run_guard.summary()
        trace = episode.trace.model_copy(update={"run_id": record.run_id})
        trace.write_jsonl(directory / TRACE_FILE)
    finally:
        episode.close()


def _in_container(
    task: TaskSpec,
    prompt: str,
    caps: Caps,
    tracer: Tracer,
    isolation: Isolation,
    call: RetryingModel,
    record: RunRecord,
    directory: Path,
) -> None:
    """The world in a locked-down container, over stdio (:mod:`runner.isolation`).

    A container that fails (killed, timed out, a bad ``episode.json``) leaves an error
    run with the reason and no trace; a resume runs it again.
    """
    container = ContainerEpisode(task, directory, isolation, task_hash=record.task_hash)
    try:
        container.prepare()
        agent = partial(
            run_agent,
            container,
            task,
            task.instruction,
            call,
            prompt=prompt,
            caps=caps,
            tracer=tracer,
        )
        result: AgentResult | None = None
        broken: BaseException | None = None
        try:
            result = anyio.run(agent)
        except RunFailed as error:
            record.error = str(error)
        except CONTAINER_ERRORS as error:
            broken = error
        record.retries = call.retried
        container.finish()
        if broken is not None:
            msg = f"the connection to the episode broke: {broken}"
            raise EpisodeFailed(msg)
        output = container.read_output()
        if result is not None:
            _completed(record, output.grade, result)
        trace = container.trace(output).model_copy(update={"run_id": record.run_id})
        trace.write_jsonl(directory / TRACE_FILE)
    except EpisodeFailed as error:
        record.status = "error"
        record.grade = None
        record.error = f"episode container: {error}"
    finally:
        container.cleanup()


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
    "extra_set",
    "load_records",
    "manifest_json",
    "read_manifest",
    "record_json",
    "resolve_task_set",
    "run_id_for",
    "select_tasks",
    "slug",
    "task_hash",
    "task_set_version",
]
