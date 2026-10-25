"""``sandbox-train``: build and check the action dataset, run backends, calibrate, report.

    sandbox-train dataset build --config full --out training/data/out --workers 6 --decontaminate
    sandbox-train dataset check training/data/out/actions-v0.1
    sandbox-train dataset hidden-check training/data/out/actions-v0.1     (HIDDEN_TASKS_DIR)
    sandbox-train dataset card training/data/out/actions-v0.1 --into training/data/actions-v0.1
    sandbox-train dataset sft training/data/out/actions-v0.1 --out training/data/out/sft
    sandbox-train backends
    sandbox-train eval --backend rules --data training/data/out/actions-v0.1 --out r.json
    sandbox-train eval --backend jev --data ... --hidden --out jev-hidden.json   (aggregates only)
    sandbox-train calibrate --predictions val.jsonl --out calibration.json
    sandbox-train report r.json ... --out training/data/out/report --updated 2026-11-01

Hidden-set commands print and write counts and aggregate scores only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kit.rng import SeededRNG

from .baselines.config import build_backend, configs, describe, load_config
from .baselines.system_one import ConfigError
from .data.build import BuildConfig, build, read_manifest, read_split
from .data.card import render_card
from .data.example import SPLITS, ActionExample
from .data.hidden import HiddenRowsError, hidden_rows
from .data.leaks import check_dataset, check_hidden
from .eval.calibration import CalibratedBackend, Calibration, fit
from .eval.metrics import breakdown, robustness
from .eval.prices import load_prices
from .eval.report import EVAL_FORMAT, write_report
from .eval.run import Prediction, predict
from .guard_model.export import MAX_PROMPT_CHARS, export


def _say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------------- dataset
def _build(args: argparse.Namespace) -> int:
    config = BuildConfig.load(args.config)
    manifest = build(
        config,
        args.out,
        workers=args.workers,
        decontaminate=args.decontaminate,
        progress=_say,
    )
    for split in SPLITS:
        counts = manifest["splits"][split]
        _say(f"{split}: {counts['rows']} rows, {counts['families']} families")
    _say(f"wrote {args.out / config.name}")
    return 0


def _check(args: argparse.Namespace) -> int:
    problems = check_dataset(args.dataset)
    for problem in problems:
        _say(f"  {problem}")
    _say(f"{len(problems)} problem(s) in {args.dataset}")
    return 1 if problems else 0


def _hidden_check(args: argparse.Namespace) -> int:
    try:
        found = check_hidden(args.dataset)
    except HiddenRowsError as error:
        _say(str(error))
        return 2
    for world, counts in found.worlds.items():
        _say(
            f"{world}: {counts['hidden_rows']} hidden rows from {counts['hidden_tasks']} tasks; "
            f"{counts['near_duplicate_rows']} dataset rows are near-duplicates of one"
        )
    _say(f"canaries in the dataset files: {found.canaries}")
    return 1 if found.near_duplicates or found.canaries else 0


def _card(args: argparse.Namespace) -> int:
    manifest = read_manifest(args.dataset)
    args.into.mkdir(parents=True, exist_ok=True)
    (args.into / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (args.into / "DATASET_CARD.md").write_text(render_card(manifest), encoding="utf-8")
    _say(f"wrote {args.into / 'DATASET_CARD.md'} and its manifest")
    return 0


def _sft(args: argparse.Namespace) -> int:
    counts = export(args.dataset, args.out, max_prompt_chars=args.max_prompt_chars)
    for split, numbers in counts.items():
        _say(f"{split}: {numbers['written']} written, {numbers['skipped_too_long']} too long")
    return 0


# ---------------------------------------------------------------------- eval
def _backends(_: argparse.Namespace) -> int:
    for name in configs():
        config = load_config(name)
        _say(f"  {name:20} {config.kind}")
    return 0


def _sample(rows: list[ActionExample], limit: int | None, seed: int) -> list[ActionExample]:
    rows = sorted(rows, key=lambda row: row.id)
    if limit is None or limit >= len(rows):
        return rows
    return sorted(SeededRNG(seed).child("eval/sample").sample(rows, limit), key=lambda r: r.id)


def _hidden_bucket(manifest: dict[str, Any]) -> dict[str, str]:
    """Each public family's split, by member: where a hidden task's family was."""
    return {
        member: group["split"]
        for group in manifest["families"].values()
        for member in group["members"]
    }


MIN_HIDDEN_FAMILIES = 3
"""A hidden-set bucket with fewer families is reported as counts only: a score over one or
two hidden tasks' families would be close to a per-task result."""


def _bucket(members: list[Prediction], price: Any) -> dict[str, Any]:
    families = len({p.family for p in members})
    if families < MIN_HIDDEN_FAMILIES:
        return {"suppressed": True, "rows": len(members), "families": families}
    return breakdown(members, price)


def _eval(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.backend, args.set)
        backend = build_backend(config)
    except ConfigError as error:
        _say(str(error))
        return 2
    calibration = Calibration.read(args.calibration) if args.calibration else None
    if calibration is not None:
        backend = CalibratedBackend(backend, calibration)
    manifest = read_manifest(args.data)
    price = load_prices().get(config.name)
    result: dict[str, Any] = {
        "format": EVAL_FORMAT,
        "backend": describe(config),
        "dataset": manifest["name"],
        "calibration": calibration.model_dump(mode="json") if calibration else None,
        "price": price.model_dump(mode="json") if price else None,
    }
    if args.hidden:
        if args.predictions:
            _say("--predictions is refused with --hidden: hidden results are aggregates only")
            return 2
        rows: list[ActionExample] = []
        try:
            for world in manifest["config"]["worlds"]:
                rows += hidden_rows(
                    world,
                    recent=manifest["config"]["recent_steps"],
                    max_chars=manifest["config"]["max_response_chars"],
                ).rows
        except HiddenRowsError as error:
            _say(str(error))
            return 2
        predictions = predict(backend, rows)
        bucket = _hidden_bucket(manifest)
        by_bucket: dict[str, list[Prediction]] = defaultdict(list)
        for prediction in predictions:
            by_bucket[bucket.get(prediction.family, "no_rows")].append(prediction)
        result |= {
            "set": "hidden",
            "rows": len(predictions),
            "metrics": {
                "all": breakdown(predictions, price),
                **{name: _bucket(members, price) for name, members in sorted(by_bucket.items())},
            },
            "robustness": robustness(predictions),
        }
    else:
        rows = _sample(read_split(args.data, args.split), args.limit, args.seed)
        predictions = predict(backend, rows)
        result |= {
            "set": args.split,
            "rows": len(predictions),
            "metrics": breakdown(predictions, price),
            "robustness": robustness(predictions),
        }
        if args.predictions:
            args.predictions.parent.mkdir(parents=True, exist_ok=True)
            with args.predictions.open("w", encoding="utf-8") as handle:
                for prediction in predictions:
                    handle.write(prediction.model_dump_json() + "\n")
    result["model_versions"] = sorted(
        {p.result.model_version for p in predictions if p.result.model_version}
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    overall = result["metrics"]["all"]["overall"] if args.hidden else result["metrics"]["overall"]
    accuracy = overall["accuracy"]["value"]
    _say(
        f"{config.name} on {result['set']}: {result['rows']} rows, accuracy "
        f"{'n/a' if accuracy is None else f'{100 * accuracy:.1f}%'}; wrote {args.out}"
    )
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    with args.predictions.open(encoding="utf-8") as handle:
        predictions = [Prediction.model_validate_json(line) for line in handle if line.strip()]
    splits = {p.split for p in predictions}
    if splits != {"validation"}:
        _say(f"calibrate on validation predictions only; these are from {sorted(map(str, splits))}")
        return 2
    backend = predictions[0].result.backend if predictions else "unknown"
    calibration = fit(
        backend, predictions, target_missed=args.target_missed, fitted_on=str(args.predictions)
    )
    calibration.save(args.out)
    _say(
        f"temperature {calibration.temperature}, threshold {calibration.threshold:.3f}, "
        f"ECE {calibration.ece_before} -> {calibration.ece_after}; wrote {args.out}"
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    written = write_report(args.results, args.out, args.updated)
    for kind, path in written.items():
        _say(f"{kind}: {path}")
    return 0


# ----------------------------------------------------------------- argparse
def parser() -> argparse.ArgumentParser:
    """The command line."""
    top = argparse.ArgumentParser(prog="sandbox-train", description=__doc__.split("\n")[0])
    commands = top.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("dataset", help="build and check the action dataset")
    steps = dataset.add_subparsers(dest="step", required=True)
    one = steps.add_parser("build", help="build a dataset")
    one.add_argument("--config", default="full", help="ci, full, or a YAML file")
    one.add_argument("--out", type=Path, default=Path("training/data/out"))
    one.add_argument("--workers", type=int, default=1)
    one.add_argument(
        "--decontaminate",
        action="store_true",
        help="drop rows that are near-duplicates of hidden rows (needs HIDDEN_TASKS_DIR)",
    )
    one.set_defaults(run=_build)
    one = steps.add_parser("check", help="CI leakage check: canaries, hidden ids, splits")
    one.add_argument("dataset", type=Path)
    one.set_defaults(run=_check)
    one = steps.add_parser("hidden-check", help="near-duplicates of the hidden set (local)")
    one.add_argument("dataset", type=Path)
    one.set_defaults(run=_hidden_check)
    one = steps.add_parser("card", help="write the dataset card and manifest")
    one.add_argument("dataset", type=Path)
    one.add_argument("--into", type=Path, required=True)
    one.set_defaults(run=_card)
    one = steps.add_parser("sft", help="write the training scripts' prompt-completion files")
    one.add_argument("dataset", type=Path)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--max-prompt-chars", type=int, default=MAX_PROMPT_CHARS)
    one.set_defaults(run=_sft)

    one = commands.add_parser("backends", help="list the packaged backend configs")
    one.set_defaults(run=_backends)

    one = commands.add_parser("eval", help="run one backend over a split or the hidden set")
    one.add_argument("--backend", required=True, help="a packaged config name or a YAML file")
    one.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    one.add_argument("--data", type=Path, required=True, help="a built dataset folder")
    one.add_argument("--split", choices=["validation", "test"], default="test")
    one.add_argument("--hidden", action="store_true", help="the hidden set (aggregates only)")
    one.add_argument("--limit", type=int, default=None, help="a seeded sample of N rows")
    one.add_argument("--seed", type=int, default=0)
    one.add_argument("--calibration", type=Path, default=None)
    one.add_argument("--predictions", type=Path, default=None)
    one.add_argument("--out", type=Path, required=True)
    one.set_defaults(run=_eval)

    one = commands.add_parser("calibrate", help="fit a calibration on validation predictions")
    one.add_argument("--predictions", type=Path, required=True)
    one.add_argument("--target-missed", type=float, default=0.02)
    one.add_argument("--out", type=Path, required=True)
    one.set_defaults(run=_calibrate)

    one = commands.add_parser("report", help="the eval report and guards/results.json")
    one.add_argument("results", type=Path, nargs="+")
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--updated", type=dt.date.fromisoformat, required=True)
    one.set_defaults(run=_report)
    return top


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = parser().parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
