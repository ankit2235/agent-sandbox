"""Seeded task templates: more training tasks from a public task by consistent substitution.

A template lives beside a world's public tasks, at ``<tasks_dir>/templates/<base>.json``
(JSON, so the ``*.yaml`` task loaders never mistake it for a task)::

    {
      "format": "task-template-v1",
      "base": "commerce-ambiguous-size-ask",
      "summary": "Other shoppers, shirts and sizes.",
      "variants": 12,
      "reseed": true,
      "slots": {
        "shopper": [{}, {"Asha Example": "Ravi Example", "asha@sandbox": "ravi@sandbox"}],
        "shirt": [{}, {"Cotton T-shirt": "Linen shirt", "59900": "74900", "₹599.00": "₹749.00"}]
      }
    }

Each slot lists choices; a choice is a set of literal replacements, and ``{}`` keeps the
base task's text. A variant takes one choice per slot (a seeded sample of the
combinations, never the all-``{}`` one) and applies every replacement at once to the
base task *and* its run file, so the golden and wrong runs move with the task. With
``reseed``, each variant also gets its own task seed (drawn from the build seed), so a
world's seeded content (ids, background noise) differs too. A variant
is kept only if its golden run still passes and its wrong runs still fail as declared
(:func:`training.data.labels.walk`); graders that name a payee or a host keep their
meaning because a template never replaces what a grader names.

A variant's id is ``<base>.tpl-<n>``; its family is the base task's.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kit.rng import SeededRNG
from kit.scripted import RunFile, load_runs
from kit.types import TaskSpec
from kit.world import WorldSpec

FORMAT = "task-template-v1"
DIRECTORY = "templates"
MAX_COMBINATIONS = 10_000
UNCHANGED = ("world", "version", "category", "difficulty", "grader", "visibility")


class TemplateError(ValueError):
    """A template file is malformed or does not fit its base task."""


class Template(BaseModel):
    """One template file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["task-template-v1"]
    base: str
    summary: str
    variants: int = Field(default=12, ge=1, le=200)
    reseed: bool = False
    slots: dict[str, list[dict[str, str]]] = Field(min_length=1)

    @field_validator("slots")
    @classmethod
    def _choices(cls, value: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        seen: dict[str, str] = {}
        for slot, choices in value.items():
            if len(choices) < 2:
                msg = f"slot {slot!r} needs at least two choices"
                raise ValueError(msg)
            for choice in choices:
                for literal in choice:
                    if not literal:
                        msg = f"slot {slot!r} replaces an empty string"
                        raise ValueError(msg)
                    if seen.get(literal, slot) != slot:
                        msg = f"{literal!r} is replaced by slots {seen[literal]!r} and {slot!r}"
                        raise ValueError(msg)
                    seen[literal] = slot
        return value


@dataclass(frozen=True, slots=True)
class Variant:
    """One task made from a template, with its runs."""

    number: int
    task: TaskSpec
    runs: RunFile


def directory(spec: WorldSpec) -> Path | None:
    """Where a world's templates live, if it ships any."""
    if spec.tasks_dir is None:
        return None
    found = spec.tasks_dir / DIRECTORY
    return found if found.is_dir() else None


def load(path: Path) -> Template:
    """Read and check one template file."""
    try:
        template = Template.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, json.JSONDecodeError) as error:
        msg = f"{path}: {error}"
        raise TemplateError(msg) from error
    if path.stem != template.base:
        msg = f"{path}: the file must be named after its base task, {template.base}.json"
        raise TemplateError(msg)
    return template


def load_all(spec: WorldSpec) -> list[Template]:
    """Every template a world ships, by base task id."""
    folder = directory(spec)
    if folder is None:
        return []
    return [load(path) for path in sorted(folder.glob("*.json"))]


def _escaped(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


def substitute(value: Any, replacements: dict[str, str]) -> Any:
    """``value`` with every literal replaced at once (longest first), through its JSON text."""
    if not replacements:
        return value
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    table = {_escaped(old): _escaped(new) for old, new in replacements.items()}
    pattern = re.compile("|".join(re.escape(old) for old in sorted(table, key=len, reverse=True)))
    return json.loads(pattern.sub(lambda match: table[match.group(0)], text))


def _combinations(template: Template, seed: int) -> list[tuple[int, ...]]:
    sizes = [len(choices) for choices in template.slots.values()]
    total = math.prod(sizes)
    if total > MAX_COMBINATIONS:
        msg = f"{template.base}: {total} combinations; keep a template under {MAX_COMBINATIONS}"
        raise TemplateError(msg)
    choices = list(template.slots.values())
    combos = [
        combo
        for combo in itertools.product(*(range(size) for size in sizes))
        if any(choices[slot][pick] for slot, pick in enumerate(combo))
    ]
    if len(combos) > template.variants:
        combos = (
            SeededRNG(seed).child(f"template/{template.base}").sample(combos, template.variants)
        )
    return sorted(combos)


def variants(spec: WorldSpec, template: Template, seed: int) -> list[Variant]:
    """The template's variants, before their runs are checked."""
    if spec.runs_dir is None:
        msg = f"world {spec.name!r} ships no run files for templates to follow"
        raise TemplateError(msg)
    try:
        base = spec.task_source().get(template.base)
    except KeyError:
        msg = f"template base {template.base!r} is not a public {spec.name} task"
        raise TemplateError(msg) from None
    runs = load_runs(base.id, spec.runs_dir)
    base_text = json.dumps(
        [base.model_dump(mode="json"), runs.model_dump(mode="json", exclude_none=True)],
        ensure_ascii=False,
        sort_keys=True,
    )
    for slot, choices in template.slots.items():
        for choice in choices:
            missing = [literal for literal in choice if _escaped(literal) not in base_text]
            if missing:
                msg = f"{template.base}: slot {slot!r} replaces {missing}, not in the base task"
                raise TemplateError(msg)
    out: list[Variant] = []
    slots = list(template.slots.values())
    for number, combo in enumerate(_combinations(template, seed), start=1):
        replacements: dict[str, str] = {}
        for index, pick in enumerate(combo):
            replacements |= slots[index][pick]
        task_id = f"{base.id}.tpl-{number:03d}"
        raw = substitute(base.model_dump(mode="json"), replacements) | {"id": task_id}
        task = TaskSpec.model_validate(raw)
        changed = [name for name in UNCHANGED if getattr(task, name) != getattr(base, name)]
        if task.seed != base.seed:
            changed.append("seed")
        if changed:
            msg = f"{template.base}: a replacement changes the task's {', '.join(changed)}"
            raise TemplateError(msg)
        if template.reseed:
            reseeded = SeededRNG(seed).child(f"template/{base.id}/{number}").randint(0, 2**31)
            task = task.model_copy(update={"seed": reseeded})
        run_raw = substitute(runs.model_dump(mode="json", exclude_none=True), replacements)
        out.append(Variant(number, task, RunFile.model_validate(run_raw | {"task": task_id})))
    return out


__all__ = [
    "DIRECTORY",
    "FORMAT",
    "Template",
    "TemplateError",
    "Variant",
    "directory",
    "load",
    "load_all",
    "substitute",
    "variants",
]
