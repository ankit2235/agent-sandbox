"""Eval result files, and the report and leaderboard file made from them.

``sandbox-train eval`` writes one ``guard-eval-v1`` file per backend and row set: the
public test split (or validation), or the hidden set. A hidden-set file holds aggregates
only, split by whether each hidden task's family was in train, validation or test,
because the hidden set is built from public families (docs/HIDDEN_SETS.md).

``sandbox-train report`` reads several of them and writes:

- ``report.json``: every backend's numbers side by side;
- ``eval-report.md``: the tables for ``docs/blog/guard-model.md``, opening with where
  our model loses;
- ``guards/results.json``: the Guard models leaderboard file (``guard-results-v0.1``,
  ``schemas/guard-results.schema.json``).

Given guarded sandbox run directories (``--runs``, from ``sandbox run --guard``), each
backend's entry also gets ``with_guard``: by world and task set, the agent's task success
with the guard on (and, given an unguarded run of the same agent on the same tasks, without
it), next to the guard's block and ask_user rates, and whether every run's decision records
verified. A hidden set gives aggregates only.

Nothing is invented: a metric that was not measured is null, and the report says which
backends are ours.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import jsonschema

from kit.world import worlds
from runner.runs import RunRecord, load_records, read_manifest
from runner.score import score
from runner.verify import verify

EVAL_FORMAT = "guard-eval-v1"
RESULTS_FORMAT = "guard-results-v0.1"
SCHEMA = Path(__file__).parent / "schemas" / "guard-results.schema.json"
OURS = "guard_model"
DISCLOSURE = (
    "The agent-sandbox maintainers built the guard model and the benchmark it is scored on. "
    "Every backend gets identical inputs; results reproduce from the published configs."
)
HIGHER = ("accuracy", "correct_block_rate", "stopped_rate")
LOWER = ("missed_mistake_rate", "wrongful_block_rate", "needless_ask_rate", "error_rate", "ece")


def read_result(path: Path) -> dict[str, Any]:
    """One eval result file, checked to be one."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != EVAL_FORMAT:
        msg = f"{path} is not a {EVAL_FORMAT} file"
        raise ValueError(msg)
    return data


def _value(metric: Any) -> float | None:
    if isinstance(metric, dict):
        value = metric.get("value")
        return float(value) if isinstance(value, int | float) else None
    return float(metric) if isinstance(metric, int | float) else None


def _overall(result: dict[str, Any]) -> dict[str, Any]:
    if result["set"] == "hidden":
        return dict(result["metrics"].get("all", {}).get("overall", {}))
    return dict(result["metrics"]["overall"])


def losses(results: list[dict[str, Any]]) -> list[str]:
    """Each metric and set where another backend beats ours, as a sentence."""
    lines: list[str] = []
    for ours in (r for r in results if r["backend"]["kind"] == OURS):
        for other in results:
            if other is ours or other["set"] != ours["set"] or other["backend"]["kind"] == OURS:
                continue
            mine, theirs = _overall(ours), _overall(other)
            for metric in (*HIGHER, *LOWER):
                a, b = _value(mine.get(metric)), _value(theirs.get(metric))
                if a is None or b is None:
                    continue
                if (metric in HIGHER and b > a) or (metric in LOWER and b < a):
                    lines.append(
                        f"{ours['set']}: {other['backend']['name']} beats "
                        f"{ours['backend']['name']} on {metric} ({b:.3f} vs {a:.3f})"
                    )
    return lines


def _row(result: dict[str, Any]) -> dict[str, Any]:
    overall = _overall(result)
    return {
        "set": result["set"],
        "rows": result["rows"],
        **{metric: overall.get(metric) for metric in (*HIGHER, *LOWER, "ask_user_rate")},
        "latency_ms": overall.get("latency_ms"),
        "cost_per_1000_usd": overall.get("cost_per_1000_usd"),
        "robustness": result.get("robustness"),
        "by_bucket": (
            {k: v.get("overall", v) for k, v in result["metrics"].items() if k != "all"}
            if result["set"] == "hidden"
            else None
        ),
    }


def _share(metric: dict[str, float]) -> dict[str, float | None]:
    """A runner score (in percent) as a share, with its interval."""
    ci = metric.get("ci")
    return {"value": metric["value"] / 100, "ci": None if ci is None else ci / 100}


def _task_success(run_dir: Path, records: Sequence[RunRecord]) -> dict[str, float | None]:
    manifest = read_manifest(run_dir)
    assert manifest is not None
    return _share(score(worlds.get(manifest.config.world), records).metrics["overall"])


def guard_runs(
    run_dirs: Sequence[Path], baselines: Sequence[Path] = ()
) -> dict[str, dict[str, Any]]:
    """Each guarded run directory as a ``with_guard`` cell, by backend: one per world, task
    set and agent.
    """
    unguarded: dict[tuple[str, str, str], Path] = {}
    for path in baselines:
        manifest = read_manifest(path)
        if manifest is None or manifest.config.guard is not None:
            msg = f"{path} is not an unguarded run directory"
            raise ValueError(msg)
        key = (manifest.config.world, manifest.config.tasks, manifest.config.model)
        unguarded[key] = path
    out: dict[str, dict[str, Any]] = {}
    for path in run_dirs:
        manifest = read_manifest(path)
        if manifest is None or manifest.config.guard is None:
            msg = f"{path} is not a guarded run directory (sandbox run --guard)"
            raise ValueError(msg)
        config, guard = manifest.config, manifest.config.guard
        records = [r for r in load_records(path) if r.status == "completed"]
        if not records:
            msg = f"{path} has no completed runs"
            raise ValueError(msg)
        decided = [r.guard for r in records if r.guard is not None]
        decisions = sum(g.decisions for g in decided)
        baseline = unguarded.get((config.world, config.tasks, config.model))
        kind = guard.backend_config.get("kind")
        if kind is None:
            msg = f"{path}: the backend's kind is not recorded (backend_config has no 'kind')"
            raise ValueError(msg)
        entry = out.setdefault(
            guard.backend,
            {
                "kind": kind,
                "model": guard.backend_config.get("model")
                or guard.backend_config.get("base_model"),
                "with_guard": [],
            },
        )
        cell = (config.world, config.tasks, config.model_name)
        if any((c["world"], c["task_set"], c["agent"]) == cell for c in entry["with_guard"]):
            msg = f"two guarded run directories for {guard.backend} on {cell}; give one"
            raise ValueError(msg)
        entry["with_guard"].append(
            {
                "world": config.world,
                "task_set": config.tasks,
                "agent": config.model_name,
                "policy": {
                    "id": guard.policy_id,
                    "version": guard.policy_version,
                    "hash": guard.policy_hash,
                },
                "runs": len(records),
                "tasks": len({r.task_id for r in records}),
                "task_success": _task_success(path, records),
                "task_success_without_guard": (
                    _task_success(
                        baseline, [r for r in load_records(baseline) if r.status == "completed"]
                    )
                    if baseline is not None
                    else None
                ),
                "decisions": decisions,
                "block_rate": sum(g.blocked for g in decided) / decisions if decisions else None,
                "ask_user_rate": sum(g.asked for g in decided) / decisions if decisions else None,
                "records_verified": verify(path).ok,
                "hidden": any(r.visibility == "hidden" for r in records),
            }
        )
    for entry in out.values():
        entry["with_guard"].sort(key=lambda c: (c["world"], c["task_set"], c["agent"]))
    return out


def leaderboard(
    results: list[dict[str, Any]],
    updated: dt.date,
    runs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The Guard models leaderboard file."""
    backends: dict[str, dict[str, Any]] = {}
    for result in results:
        backend = result["backend"]
        entry = backends.setdefault(
            backend["name"],
            {
                "name": backend["name"],
                "kind": backend["kind"],
                "ours": backend["kind"] == OURS,
                "model": backend.get("model") or backend.get("base_model"),
                "model_versions": [],
                "price": result.get("price"),
                "sets": {},
            },
        )
        entry["model_versions"] = sorted({*entry["model_versions"], *result["model_versions"]})
        entry["sets"][result["set"]] = _row(result)
    for name, guarded in (runs or {}).items():
        entry = backends.setdefault(
            name,
            {
                "name": name,
                "kind": guarded["kind"],
                "ours": guarded["kind"] == OURS,
                "model": guarded["model"],
                "model_versions": [],
                "price": None,
                "sets": {},
            },
        )
        entry["with_guard"] = guarded["with_guard"]
    datasets = sorted({result["dataset"] for result in results})
    data = {
        "format": RESULTS_FORMAT,
        "updated": updated.isoformat(),
        "dataset": datasets[0] if len(datasets) == 1 else datasets,
        "disclosure": DISCLOSURE,
        "backends": [backends[name] for name in sorted(backends)],
    }
    jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    return data


def _pct(metric: Any) -> str:
    value = _value(metric)
    if value is None:
        return "—"
    ci = metric.get("ci") if isinstance(metric, dict) else None
    return f"{100 * value:.1f}%" + (f" ± {100 * ci:.1f}" if isinstance(ci, int | float) else "")


def guard_table(runs: dict[str, dict[str, Any]]) -> list[str]:
    """The guarded sandbox runs as a table: task success always beside the block rate."""
    if not runs:
        return []
    lines = [
        "",
        "## With the guard on (sandbox runs)",
        "",
        "| Backend | World | Task set | Agent | Task success | Without the guard | Block rate | "
        "Ask rate | Decisions | Records verified |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, entry in sorted(runs.items()):
        for cell in entry["with_guard"]:
            block, ask = cell["block_rate"], cell["ask_user_rate"]
            lines.append(
                f"| {name} | {cell['world']} | {cell['task_set']} | {cell['agent']} | "
                f"{_pct(cell['task_success'])} | {_pct(cell['task_success_without_guard'])} | "
                f"{'—' if block is None else f'{100 * block:.1f}%'} | "
                f"{'—' if ask is None else f'{100 * ask:.1f}%'} | {cell['decisions']} | "
                f"{'yes' if cell['records_verified'] else 'NO'} |"
            )
    return lines


def markdown(results: list[dict[str, Any]], runs: dict[str, dict[str, Any]] | None = None) -> str:
    """The report's tables, opening with where our model loses."""
    lines = ["## Where our model loses", ""]
    if not any(r["backend"]["kind"] == OURS for r in results):
        lines.append("Our model is not in these results.")
    else:
        lines += [f"- {line}" for line in losses(results)] or ["- Nowhere in these results."]
    for name in sorted({r["set"] for r in results}):
        lines += ["", f"## {name}", ""]
        lines.append(
            "| Backend | Rows | Accuracy | Mistakes stopped | Missed | Wrongful blocks | "
            "Needless asks | Ask rate | ECE | p50 ms | p99 ms | $/1k |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for result in sorted(
            (r for r in results if r["set"] == name), key=lambda r: r["backend"]["name"]
        ):
            o = _overall(result)
            latency = o.get("latency_ms") or {}
            cost = o.get("cost_per_1000_usd")
            ece = o.get("ece")
            p50, p99 = latency.get("p50"), latency.get("p99")
            lines.append(
                f"| {result['backend']['name']} | {result['rows']} | {_pct(o.get('accuracy'))} | "
                f"{_pct(o.get('stopped_rate'))} | {_pct(o.get('missed_mistake_rate'))} | "
                f"{_pct(o.get('wrongful_block_rate'))} | {_pct(o.get('needless_ask_rate'))} | "
                f"{_pct(o.get('ask_user_rate'))} | "
                f"{'—' if ece is None else f'{ece:.3f}'} | "
                f"{'—' if p50 is None else f'{p50:.0f}'} | "
                f"{'—' if p99 is None else f'{p99:.0f}'} | "
                f"{'—' if cost is None else f'{cost:.4f}'} |"
            )
    lines += guard_table(runs or {})
    return "\n".join(lines) + "\n"


def write_report(
    paths: list[Path],
    out: Path,
    updated: dt.date,
    *,
    runs: Sequence[Path] = (),
    baselines: Sequence[Path] = (),
) -> dict[str, Path]:
    """Read eval results (and guarded run directories) and write the report, its tables and
    the leaderboard file.
    """
    results = [read_result(path) for path in paths]
    guarded = guard_runs(runs, baselines)
    out.mkdir(parents=True, exist_ok=True)
    written = {
        "report": out / "report.json",
        "markdown": out / "eval-report.md",
        "leaderboard": out / "guards" / "results.json",
    }
    written["report"].write_text(
        json.dumps({"results": results, "losses": losses(results)}, indent=2) + "\n",
        encoding="utf-8",
    )
    written["markdown"].write_text(markdown(results, guarded), encoding="utf-8")
    written["leaderboard"].parent.mkdir(parents=True, exist_ok=True)
    written["leaderboard"].write_text(
        json.dumps(leaderboard(results, updated, guarded), indent=2) + "\n", encoding="utf-8"
    )
    return written


__all__ = [
    "DISCLOSURE",
    "EVAL_FORMAT",
    "RESULTS_FORMAT",
    "SCHEMA",
    "guard_runs",
    "guard_table",
    "leaderboard",
    "losses",
    "markdown",
    "read_result",
    "write_report",
]
