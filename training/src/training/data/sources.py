"""Where rows come from: public tasks, template variants, and either under stress modifiers.

The build is a list of :class:`Job` s, each one task (a public task, or a template
variant of one) optionally under one or two of its world's stress modifiers with a
seed. A job plays every run of its task (:func:`training.data.labels.walk`) and labels
every high-impact attempt. Jobs are plain data so they can run in worker processes; a
job whose modifiers do not apply to its task gives nothing and is counted.

Under stress, a task's wrong runs are its modifiers' baits, named ``wrong_<modifier>``
by :func:`stress.core.scripted_runs`: an agent that falls for misleading content, a
look-alike payee or a planted note. They are the dataset's hard cases.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import jsonschema

from kit.scripted import GOLDEN, RunFile, load_runs
from kit.types import TaskSpec
from kit.world import WorldSpec, worlds
from runner.hidden import content_hash
from stress.core import NotApplicable, apply, registry, scripted_runs

from .. import DATASET_VERSION
from ..world import WorldActions, actions
from . import templates
from .example import ActionExample, RunKind, Source, example_id
from .labels import golden_asks, label, walk


@dataclass(frozen=True, slots=True)
class Job:
    """One task to play: which base, which variant, which modifiers and seed."""

    world: str
    base: str
    variant: int | None = None
    stress: tuple[str, ...] = ()
    stress_seed: int | None = None


@dataclass(slots=True)
class JobResult:
    """What one job gave."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    applicable: bool = True
    runs: int = 0
    misbehaved: int = 0
    """Runs that did not go as scripted or did not end as declared: no rows from them."""
    golden_not_allowed: int = 0
    """Golden-run attempts the graders would not allow: dropped, never relabelled."""


@dataclass(frozen=True, slots=True)
class Settings:
    """The parts of the build config a job needs."""

    seed: int
    template_variants: int | None
    recent_steps: int
    max_response_chars: int


@cache
def _spec(world: str) -> WorldSpec:
    return worlds.get(world)


@cache
def _actions(world: str) -> WorldActions:
    return actions.get(world)


@cache
def _public(world: str) -> dict[str, TaskSpec]:
    spec = _spec(world)
    return {task.id: task for task in spec.task_source().split(spec.version)}


@cache
def _variants(world: str, base: str, seed: int, limit: int | None) -> dict[int, templates.Variant]:
    spec = _spec(world)
    found = {template.base: template for template in templates.load_all(spec)}
    template = found[base]
    if limit is not None:
        template = template.model_copy(update={"variants": min(limit, template.variants)})
    return {variant.number: variant for variant in templates.variants(spec, template, seed)}


def base_task(job: Job, settings: Settings) -> tuple[TaskSpec, RunFile]:
    """The unstressed task a job starts from, and its runs."""
    spec = _spec(job.world)
    if job.variant is None:
        task = _public(job.world)[job.base]
        assert spec.runs_dir is not None
        return task, load_runs(task.id, spec.runs_dir)
    variant = _variants(job.world, job.base, settings.seed, settings.template_variants)[job.variant]
    return variant.task, variant.runs


def run_job(job: Job, settings: Settings) -> JobResult:
    """Play one job's runs and label every attempt."""
    spec = _spec(job.world)
    world_actions = _actions(job.world)
    task, runs = base_task(job, settings)
    out = JobResult()
    if job.stress:
        assert job.stress_seed is not None
        try:
            task = apply(task, list(job.stress), job.stress_seed)
            runs = scripted_runs(task, runs)
        except NotApplicable:
            out.applicable = False
            return out
    try:
        spec.validate_task(task)
    except (ValueError, jsonschema.ValidationError):
        out.applicable = False
        return out
    asks = golden_asks(dict(runs.runs), world_actions)
    task_hash = content_hash(task)
    for name, run in runs.runs.items():
        walked = walk(
            spec,
            task,
            run,
            world_actions,
            recent=settings.recent_steps,
            max_chars=settings.max_response_chars,
        )
        out.runs += 1
        if not walked.behaved:
            out.misbehaved += 1
            continue
        kind: RunKind = "golden" if name == GOLDEN else "wrong"
        for attempt in walked.attempts:
            decided = label(attempt, world_actions, golden_asks=asks)
            if kind == "golden" and decided.decision != "allow":
                out.golden_not_allowed += 1
                continue
            source = Source(
                task_id=task.id,
                task_hash=task_hash,
                base_task=job.base,
                run=name,
                run_kind=kind,
                step=attempt.step,
                stress=list(task.stress),
                template=job.base if job.variant is not None else None,
                variant=job.variant,
                stress_seed=job.stress_seed,
            )
            row = ActionExample(
                id="pending",
                dataset=DATASET_VERSION,
                world=task.world,
                world_version=spec.version,
                family=job.base,
                category=task.category,
                difficulty=task.difficulty,
                source=source,
                request=attempt.request,
                label=decided,
            )
            row = row.model_copy(update={"id": example_id(row.text(), decided)})
            out.rows.append(row.model_dump(mode="json"))
    return out


def plan(
    world: str,
    *,
    seed: int,
    use_templates: bool,
    template_variants: int | None,
    stress_seeds: list[int],
    max_modifiers: int,
    max_tasks: int | None = None,
) -> list[Job]:
    """Every job for one world, in a fixed order."""
    spec = _spec(world)
    public = sorted(_public(world))
    if max_tasks is not None:
        public = public[:max_tasks]
    bases = [Job(world, task_id) for task_id in public]
    if use_templates:
        chosen = set(public)
        for template in templates.load_all(spec):
            if template.base not in chosen:
                continue
            numbers = _variants(world, template.base, seed, template_variants)
            bases += [Job(world, template.base, variant=number) for number in sorted(numbers)]
    names = registry.get(world).names() if world in registry.names() else []
    picks = [
        combo
        for size in range(1, max_modifiers + 1)
        for combo in itertools.combinations(names, size)
    ]
    jobs = list(bases)
    for job in bases:
        for combo in picks:
            for stress_seed in stress_seeds:
                jobs.append(
                    Job(world, job.base, job.variant, stress=combo, stress_seed=stress_seed)
                )
    return jobs


__all__ = ["Job", "JobResult", "Settings", "base_task", "plan", "run_job"]
