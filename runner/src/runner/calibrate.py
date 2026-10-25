"""``sandbox stress calibrate``: how much each modifier lowers each model's score.

For each model, three kinds of ordinary, resumable run directories:

- ``base``: the public tasks every sampled stressed task was made from, unstressed;
- one per modifier: up to ``--per-modifier`` public tasks it applies to (seeded), stressed;
- ``stressed-set``: the public stressed set, ``<world>-<version>-stressed``.

The stressed tasks are written as task files under ``<out>/tasks/`` (they are derived
from public tasks only, so they may live in a local run directory). The report compares,
per modifier and model, the base pass rate on the very tasks the modifier was applied
to with the stressed pass rate, and flags modifiers that are too easy or too hard:

- ``too_easy``: no model loses as much as ``EASY_DROP`` points;
- ``too_hard``: every model scores 0% stressed while scoring at least ``HARD_BASE`` on
  the same tasks unstressed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kit.types import TaskSpec
from kit.world import WorldSpec, worlds
from stress.core import base_id, per_modifier, registry

from .models import ModelFactory
from .runs import (
    STRESSED_SUFFIX,
    RunConfig,
    RunRecord,
    execute,
    load_records,
    slug,
    task_set_version,
)
from .score import score

REPORT_FORMAT = "stress-calibration-v1"
EASY_DROP = 5.0
HARD_BASE = 50.0
STRESSED_TARGET = 50.0
"""The phase's goal: the best tested model scores under about this on the stressed set."""
BASE = "base"
STRESSED_SET = "stressed-set"


@dataclass(frozen=True, slots=True)
class Plan:
    """What a calibration runs: the stressed tasks per modifier and their base tasks."""

    world: str
    per_modifier: int
    seed: int
    stressed: dict[str, list[TaskSpec]]

    @property
    def base_ids(self) -> list[str]:
        """Every base task a stressed task was made from."""
        return sorted({base_id(t) for tasks in self.stressed.values() for t in tasks})


def plan(spec: WorldSpec, modifiers: Sequence[str], count: int, seed: int) -> Plan:
    """Sample the stressed tasks: up to ``count`` per modifier, each with a trap run."""
    stress = registry.get(spec.name)
    names = [stress.resolve(name).name for name in (modifiers or stress.names())]
    public = spec.task_source().split(spec.version)
    picked = per_modifier(public, names, count, seed, runs_dir=spec.runs_dir)
    empty = [name for name, tasks in picked.items() if not tasks]
    if empty:
        msg = f"no public {spec.name} task can carry {empty} with a trap run"
        raise ValueError(msg)
    return Plan(spec.name, count, seed, picked)


def _write_tasks(folder: Path, tasks: Sequence[TaskSpec]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        path = folder / f"{task.id}.yaml"
        path.write_text(yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False), "utf-8")
    return folder


def calibrate(
    world: str,
    models: Sequence[str],
    out: Path,
    *,
    modifiers: Sequence[str] = (),
    per_modifier_count: int = 10,
    seed: int = 0,
    runs: int = 1,
    concurrency: int = 1,
    settings: dict[str, Any] | None = None,
    factory: ModelFactory | None = None,
    report: Callable[[RunRecord], None] | None = None,
) -> dict[str, Any]:
    """Run every model on the plan, then write and return the report."""
    spec = worlds.get(world)
    chosen = plan(spec, modifiers, per_modifier_count, seed)
    groups: dict[str, str] = {
        BASE: str(
            _write_tasks(
                out / "tasks" / BASE,
                [spec.task_source().get(task_id) for task_id in chosen.base_ids],
            )
        )
    }
    for name, tasks in chosen.stressed.items():
        groups[name] = str(_write_tasks(out / "tasks" / name, tasks))
    groups[STRESSED_SET] = task_set_version(spec) + STRESSED_SUFFIX
    for model in models:
        for group, tasks_value in groups.items():
            config = RunConfig(
                world=world,
                tasks=tasks_value,
                model=model,
                model_name=model,
                provider=model.split("/", 1)[0] if "/" in model else model.split(":", 1)[0],
                runs=runs,
                **(settings or {}),
            )
            execute(
                config,
                run_dir(out, model, group),
                concurrency=concurrency,
                factory=factory,
                report=report,
            )
    (out / "plan.json").write_text(
        json.dumps(
            {
                "world": world,
                "models": list(models),
                "per_modifier": per_modifier_count,
                "seed": seed,
                "modifiers": {name: [t.id for t in ts] for name, ts in chosen.stressed.items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return write_report(out)


def run_dir(out: Path, model: str, group: str) -> Path:
    """``<out>/runs/<model-slug>/<group>``."""
    return out / "runs" / slug(model) / group


def _rates(spec: WorldSpec, directory: Path) -> dict[str, float]:
    records = [r for r in load_records(directory) if r.status == "completed"]
    if not records:
        msg = f"{directory} has no completed runs; finish the calibration run first"
        raise ValueError(msg)
    return {task: 100 * rate for task, rate in score(spec, records).task_pass_rates.items()}


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 1)


def verdict(results: dict[str, dict[str, float]]) -> str:
    """``too_easy``, ``too_hard`` or ``ok`` for one modifier, from every model's numbers."""
    if all(r["drop"] < EASY_DROP for r in results.values()):
        return "too_easy"
    if all(r["stressed"] == 0 and r["base"] >= HARD_BASE for r in results.values()):
        return "too_hard"
    return "ok"


def build_report(out: Path) -> dict[str, Any]:
    """The report, from a calibration directory's plan and run directories."""
    meta = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    spec = worlds.get(meta["world"])
    modifiers: list[dict[str, Any]] = []
    rates = {
        model: {
            group: _rates(spec, run_dir(out, model, group))
            for group in (BASE, *meta["modifiers"], STRESSED_SET)
        }
        for model in meta["models"]
    }
    for name, task_ids in meta["modifiers"].items():
        results: dict[str, dict[str, float]] = {}
        for model in meta["models"]:
            stressed = [rates[model][name][t] for t in task_ids]
            bases = [rates[model][BASE][t.removesuffix(f".{name}")] for t in task_ids]
            results[model] = {
                "base": _mean(bases),
                "stressed": _mean(stressed),
                "drop": round(_mean(bases) - _mean(stressed), 1),
            }
        modifiers.append(
            {"name": name, "tasks": len(task_ids), "results": results, "verdict": verdict(results)}
        )
    stressed_set = {
        model: {
            "overall": _mean(list(rates[model][STRESSED_SET].values())),
            "tasks": len(rates[model][STRESSED_SET]),
        }
        for model in meta["models"]
    }
    best = max(s["overall"] for s in stressed_set.values())
    return {
        "format": REPORT_FORMAT,
        "world": spec.name,
        "version": task_set_version(spec),
        "seed": meta["seed"],
        "per_modifier": meta["per_modifier"],
        "models": meta["models"],
        "thresholds": {"easy_drop": EASY_DROP, "hard_base": HARD_BASE, "target": STRESSED_TARGET},
        "modifiers": modifiers,
        "stressed_set": stressed_set,
        "best_stressed": best,
        "best_under_target": best < STRESSED_TARGET,
    }


def markdown(report: dict[str, Any]) -> str:
    """The report as a Markdown page."""
    models = report["models"]
    lines = [
        f"# Stress calibration: {report['version']}",
        "",
        f"Seed {report['seed']}, up to {report['per_modifier']} tasks per modifier. Pass rates "
        "in %, base on the same tasks the modifier was applied to; drop in points.",
        "",
        "| modifier | tasks | "
        + " | ".join(f"{m} base | {m} stressed | {m} drop" for m in models)
        + " | verdict |",
        "| --- | --- | " + " | ".join("--- | --- | ---" for _ in models) + " | --- |",
    ]
    for row in report["modifiers"]:
        cells = " | ".join(
            f"{r['base']} | {r['stressed']} | {r['drop']}"
            for r in (row["results"][m] for m in models)
        )
        lines.append(f"| {row['name']} | {row['tasks']} | {cells} | {row['verdict']} |")
    lines += ["", f"## The stressed set ({report['version']}{STRESSED_SUFFIX})", ""]
    for model, numbers in report["stressed_set"].items():
        lines.append(f"- {model}: {numbers['overall']}% over {numbers['tasks']} tasks")
    under = "yes" if report["best_under_target"] else "no"
    lines += [
        "",
        f"Best model on the stressed set: {report['best_stressed']}% "
        f"(under {report['thresholds']['target']:g}%: {under}).",
        "",
        f"`too_easy`: every model drops less than {report['thresholds']['easy_drop']:g} points. "
        f"`too_hard`: every model scores 0% stressed and at least "
        f"{report['thresholds']['hard_base']:g}% on the same tasks unstressed.",
        "",
    ]
    return "\n".join(lines)


def write_report(out: Path) -> dict[str, Any]:
    """Write ``report.json`` and ``report.md`` from a calibration directory."""
    report = build_report(out)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(markdown(report), encoding="utf-8")
    return report


__all__ = [
    "EASY_DROP",
    "HARD_BASE",
    "REPORT_FORMAT",
    "STRESSED_TARGET",
    "Plan",
    "build_report",
    "calibrate",
    "markdown",
    "plan",
    "run_dir",
    "verdict",
    "write_report",
]
