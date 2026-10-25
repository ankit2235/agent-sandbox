"""Building the action dataset: jobs, exact de-duplication, family groups, splits, balance.

1. Plan the jobs (:mod:`training.data.sources`) and play them, in worker processes if
   asked; the result does not depend on how many.
2. Drop exact duplicates (the same rendered request); a request seen with two different
   decisions is a label conflict, and every copy of it is dropped and counted.
3. Merge families whose rows are near-duplicates across families (:mod:`.similarity`).
4. With ``decontaminate``, drop every row that is a near-duplicate of a hidden-set row;
   only the counts are kept.
5. Split by family group (:mod:`.splits`), then cap each decision class in train at
   ``max_class_ratio`` times the smallest one, per world (seeded); validation and test
   keep their natural balance.
6. Write ``train.jsonl``, ``validation.jsonl``, ``test.jsonl`` and ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kit.rng import SeededRNG

from .. import DATASET_VERSION
from .example import SPLITS, ActionExample, text_key
from .hidden import hidden_rows
from .similarity import NearDuplicateConfig, near_pairs, signatures
from .sources import Job, JobResult, Settings, plan, run_job
from .splits import Families, SplitConfig, assign

CONFIGS = Path(__file__).parent / "configs"
MANIFEST = "manifest.json"
MANIFEST_FORMAT = "actions-manifest-v1"


class BuildConfig(BaseModel):
    """Everything a build depends on; recorded in the manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = DATASET_VERSION
    seed: int = 0
    worlds: list[str] = Field(default_factory=lambda: ["payments", "security"])
    templates: bool = True
    template_variants: int | None = None
    """At most this many variants per template (None: what each template asks for)."""
    stress_seeds: list[int] = Field(default_factory=lambda: [0, 1, 2])
    max_modifiers: int = Field(default=2, ge=0, le=2)
    max_tasks: int | None = None
    """Only the first N public tasks per world, by id (a small build, for CI)."""
    recent_steps: int = Field(default=8, ge=1)
    max_response_chars: int = Field(default=2000, ge=100)
    split: SplitConfig = Field(default_factory=SplitConfig)
    near_duplicate: NearDuplicateConfig = Field(default_factory=NearDuplicateConfig)
    max_class_ratio: float = Field(default=3.0, ge=1.0)

    @classmethod
    def load(cls, name_or_path: str) -> BuildConfig:
        """A packaged config by name (``ci``, ``full``) or a YAML file."""
        path = Path(name_or_path)
        if not path.exists():
            path = CONFIGS / f"{name_or_path}.yaml"
        if not path.exists():
            known = sorted(p.stem for p in CONFIGS.glob("*.yaml"))
            msg = f"no build config {name_or_path!r}; packaged ones: {known}"
            raise FileNotFoundError(msg)
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


Progress = Callable[[str], None]


def _play(jobs: list[Job], settings: Settings, workers: int) -> list[JobResult]:
    if workers <= 1:
        return [run_job(job, settings) for job in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_job, jobs, [settings] * len(jobs), chunksize=8))


def _dedupe(rows: list[ActionExample]) -> tuple[list[ActionExample], int, int]:
    """Rows with exact duplicates removed; (kept, duplicates dropped, conflicting dropped)."""
    by_text: dict[str, list[ActionExample]] = defaultdict(list)
    for row in rows:
        by_text[text_key(row.text())].append(row)
    kept: list[ActionExample] = []
    duplicates = conflicts = 0
    for group in by_text.values():
        if len({row.label.decision for row in group}) > 1:
            conflicts += len(group)
            continue
        kept.append(group[0])
        duplicates += len(group) - 1
    return kept, duplicates, conflicts


def _balance(
    rows: list[ActionExample], seed: int, ratio: float
) -> tuple[list[ActionExample], dict[str, dict[str, int]]]:
    """Train rows with each decision capped at ``ratio`` x the rarest one, per world."""
    out: list[ActionExample] = []
    caps: dict[str, dict[str, int]] = {}
    by_world: dict[str, dict[str, list[ActionExample]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_world[row.world][row.label.decision].append(row)
    for world, classes in sorted(by_world.items()):
        cap = int(min(len(members) for members in classes.values()) * ratio)
        caps[world] = {}
        for decision, members in sorted(classes.items()):
            ordered = sorted(members, key=lambda r: r.id)
            if len(ordered) > cap:
                ordered = SeededRNG(seed).child(f"balance/{world}/{decision}").sample(ordered, cap)
            caps[world][decision] = len(ordered)
            out.extend(ordered)
    return out, caps


def _counts(rows: Iterable[ActionExample]) -> dict[str, Any]:
    rows = list(rows)

    def tally(key: Callable[[ActionExample], str]) -> dict[str, int]:
        return dict(sorted(Counter(key(row) for row in rows).items()))

    return {
        "rows": len(rows),
        "families": len({row.family for row in rows}),
        "by_world": tally(lambda r: r.world),
        "by_decision": tally(lambda r: f"{r.world}/{r.label.decision}"),
        "by_category": tally(lambda r: f"{r.world}/{r.category}"),
        "by_tool": tally(lambda r: f"{r.world}/{r.request.action.tool}"),
        "by_run_kind": tally(lambda r: r.source.run_kind),
        "stressed": sum(row.stressed for row in rows),
        "from_templates": sum(row.source.template is not None for row in rows),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(
    config: BuildConfig,
    out: Path,
    *,
    workers: int = 1,
    decontaminate: bool = False,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Build the dataset into ``out/<name>/`` and return its manifest."""
    say = progress or (lambda _message: None)
    settings = Settings(
        seed=config.seed,
        template_variants=config.template_variants,
        recent_steps=config.recent_steps,
        max_response_chars=config.max_response_chars,
    )
    jobs: list[Job] = []
    for world in config.worlds:
        jobs += plan(
            world,
            seed=config.seed,
            use_templates=config.templates,
            template_variants=config.template_variants,
            stress_seeds=config.stress_seeds,
            max_modifiers=config.max_modifiers,
            max_tasks=config.max_tasks,
        )
    say(f"{len(jobs)} jobs planned")
    results = _play(jobs, settings, workers)
    played = [ActionExample.model_validate(raw) for result in results for raw in result.rows]
    sources = {
        "jobs": len(jobs),
        "jobs_not_applicable": sum(not result.applicable for result in results),
        "public_tasks": len({job.base for job in jobs if job.variant is None}),
        "template_variants": len({(job.base, job.variant) for job in jobs if job.variant}),
        "runs_played": sum(result.runs for result in results),
        "runs_not_as_declared": sum(result.misbehaved for result in results),
        "golden_attempts_not_allowed": sum(result.golden_not_allowed for result in results),
        "attempts": len(played),
    }
    say(f"{len(played)} attempts labelled")
    rows, duplicates, conflicts = _dedupe(played)
    say(f"{len(rows)} rows after removing {duplicates} duplicates and {conflicts} conflicts")

    near = config.near_duplicate
    sigs = signatures((row.text() for row in rows), near)
    families = Families(row.family for row in rows)
    merges = 0
    for pair in near_pairs(sigs, [row.family for row in rows], near):
        merges += families.union(rows[pair.left].family, rows[pair.right].family)
    say(f"{merges} family merges from near-duplicates")

    decontamination: dict[str, Any] = {"done": decontaminate}
    if decontaminate:
        drop: set[int] = set()
        decontamination["worlds"] = {}
        for world in config.worlds:
            hidden = hidden_rows(
                world, recent=config.recent_steps, max_chars=config.max_response_chars
            )
            hidden_sigs = signatures((row.text() for row in hidden.rows), near)
            pool = [*sigs, *hidden_sigs]
            sides = ["dataset"] * len(sigs) + ["hidden"] * len(hidden_sigs)
            queries = list(range(len(sigs), len(pool)))
            found = near_pairs(pool, sides, near, queries=queries)
            before = len(drop)
            drop |= {min(pair.left, pair.right) for pair in found}
            decontamination["worlds"][world] = {
                "hidden_tasks": hidden.tasks,
                "hidden_rows": len(hidden.rows),
                "rows_dropped": len(drop) - before,
            }
        rows = [row for index, row in enumerate(rows) if index not in drop]
        say(f"{len(drop)} rows dropped as near-duplicates of hidden rows")

    group_of = {row.family: families.find(row.family) for row in rows}
    cells: dict[str, tuple[str, str]] = {}
    for row in sorted(rows, key=lambda r: r.family):
        cells.setdefault(group_of[row.family], (row.world, row.category))
    split_of = assign(cells, config.seed, config.split)
    by_split: dict[str, list[ActionExample]] = {split: [] for split in SPLITS}
    for row in rows:
        group = group_of[row.family]
        by_split[split_of[group]].append(
            row.model_copy(update={"family": group, "split": split_of[group]})
        )
    natural = _counts(by_split["train"])
    by_split["train"], caps = _balance(by_split["train"], config.seed, config.max_class_ratio)

    folder = out / config.name
    folder.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for split, members in by_split.items():
        path = folder / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in sorted(members, key=lambda r: r.id):
                handle.write(json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n")
        files[path.name] = _sha256(path)
    groups = families.groups()
    manifest = {
        "format": MANIFEST_FORMAT,
        "name": config.name,
        "config": config.model_dump(mode="json"),
        "sources": sources,
        "dedupe": {"duplicates_dropped": duplicates, "conflicting_dropped": conflicts},
        "family_merges": merges,
        "decontamination": decontamination,
        "splits": {split: _counts(members) for split, members in by_split.items()},
        "train_before_balance": natural,
        "train_class_caps": caps,
        "families": {
            group: {"split": split_of[group], "members": groups[group]}
            for group in sorted(split_of)
        },
        "files": files,
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_split(folder: Path, split: str) -> list[ActionExample]:
    """One split's rows."""
    path = folder / f"{split}.jsonl"
    with path.open(encoding="utf-8") as handle:
        return [ActionExample.model_validate_json(line) for line in handle if line.strip()]


def read_manifest(folder: Path) -> dict[str, Any]:
    """A built dataset's manifest."""
    data: dict[str, Any] = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    if data.get("format") != MANIFEST_FORMAT:
        msg = f"{folder / MANIFEST} is not an {MANIFEST_FORMAT} manifest"
        raise ValueError(msg)
    return data


__all__ = ["CONFIGS", "MANIFEST", "BuildConfig", "build", "read_manifest", "read_split"]
