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

Nothing is invented: a metric that was not measured is null, and the report says which
backends are ours.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import jsonschema

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


def leaderboard(results: list[dict[str, Any]], updated: dt.date) -> dict[str, Any]:
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


def markdown(results: list[dict[str, Any]]) -> str:
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
    return "\n".join(lines) + "\n"


def write_report(paths: list[Path], out: Path, updated: dt.date) -> dict[str, Path]:
    """Read eval results and write the report, its tables and the leaderboard file."""
    results = [read_result(path) for path in paths]
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
    written["markdown"].write_text(markdown(results), encoding="utf-8")
    written["leaderboard"].parent.mkdir(parents=True, exist_ok=True)
    written["leaderboard"].write_text(
        json.dumps(leaderboard(results, updated), indent=2) + "\n", encoding="utf-8"
    )
    return written


__all__ = [
    "DISCLOSURE",
    "EVAL_FORMAT",
    "RESULTS_FORMAT",
    "SCHEMA",
    "leaderboard",
    "losses",
    "markdown",
    "read_result",
    "write_report",
]
