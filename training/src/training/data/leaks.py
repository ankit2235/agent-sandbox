"""Leakage checks on a built dataset.

:func:`check_dataset` needs nothing private and runs in CI (``no-hidden-leak.yml``) on a
small build. It fails on:

- a canary (``runner.hidden.CANARY_RE``) or a task marked hidden anywhere in the files;
- a row whose source task id looks like a hidden one (``hidden-``) or whose task hash
  is in a published hidden hash list (``tasks/hidden-<version>.json``);
- a family in more than one split;
- a near-duplicate pair across splits (:mod:`.similarity`);
- a file whose sha256 is not the manifest's.

:func:`check_hidden` needs ``HIDDEN_TASKS_DIR`` and runs locally only. It builds the
hidden set's rows in memory (:mod:`.hidden`) and counts the dataset rows that are
near-duplicates of one. It returns counts and nothing else.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kit.world import worlds
from runner.hidden import CANARY_RE, HiddenSetError, hash_file, published

from .build import read_manifest, read_split
from .example import SPLITS, ActionExample
from .hidden import hidden_rows
from .similarity import NearDuplicateConfig, near_pairs, signatures

HIDDEN_MARK = re.compile(r"""["']?visibility["']?\s*:\s*["']?hidden\b""")
HIDDEN_ID = re.compile(r"^hidden-")


def published_hashes(world_names: list[str]) -> set[str]:
    """Every task hash in the installed worlds' published hidden hash lists."""
    found: set[str] = set()
    for name in world_names:
        spec = worlds.get(name)
        if spec.tasks_dir is not None and hash_file(spec).exists():
            try:
                found |= set(published(spec)["tasks"])
            except HiddenSetError as error:
                msg = f"{name}: the published hidden hash list is unusable: {error}"
                raise ValueError(msg) from error
    return found


def _rows(folder: Path) -> dict[str, list[ActionExample]]:
    return {split: read_split(folder, split) for split in SPLITS}


def check_dataset(folder: Path) -> list[str]:
    """Problems with a built dataset; empty if it is clean."""
    manifest = read_manifest(folder)
    problems: list[str] = []
    for name, digest in sorted(manifest["files"].items()):
        path = folder / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name}: does not match the manifest's sha256")
        else:
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if CANARY_RE.search(line):
                    problems.append(f"{name}:{number}: a hidden-set canary")
                if HIDDEN_MARK.search(line):
                    problems.append(f"{name}:{number}: a task marked hidden")
    rows = _rows(folder)
    hashes = published_hashes(sorted({row.world for members in rows.values() for row in members}))
    split_of: dict[str, set[str]] = defaultdict(set)
    for split, members in rows.items():
        for row in members:
            split_of[row.family].add(split)
            if row.split != split:
                problems.append(f"{row.id}: in {split}.jsonl but says split {row.split}")
            if HIDDEN_ID.match(row.source.task_id) or row.dataset.endswith("-hidden"):
                problems.append(f"{row.id}: comes from a task with a hidden-set id")
            if row.source.task_hash in hashes:
                problems.append(f"{row.id}: comes from a task in a published hidden set")
    for family, splits in sorted(split_of.items()):
        if len(splits) > 1:
            problems.append(f"family {family} is in {sorted(splits)}")
    near = NearDuplicateConfig.model_validate(manifest["config"]["near_duplicate"])
    flat = [row for split in SPLITS for row in rows[split]]
    sigs = signatures((row.text() for row in flat), near)
    pairs = near_pairs(sigs, [row.split or "" for row in flat], near)
    problems += [
        f"{flat[p.left].id} ({flat[p.left].split}) and {flat[p.right].id} "
        f"({flat[p.right].split}) are near-duplicates ({p.similarity:.2f})"
        for p in pairs
    ]
    return problems


@dataclass(slots=True)
class HiddenCheck:
    """What the local check against the hidden set found: counts only."""

    worlds: dict[str, dict[str, int]] = field(default_factory=dict)
    canaries: int = 0
    """Canaries found anywhere in the dataset files."""

    @property
    def near_duplicates(self) -> int:
        """Dataset rows that are near-duplicates of a hidden row, over all worlds."""
        return sum(counts["near_duplicate_rows"] for counts in self.worlds.values())


def check_hidden(folder: Path) -> HiddenCheck:
    """Count dataset rows that are near-duplicates of the hidden set's rows, per world."""
    manifest = read_manifest(folder)
    config = manifest["config"]
    near = NearDuplicateConfig.model_validate(config["near_duplicate"])
    rows = [row for members in _rows(folder).values() for row in members]
    canaries = sum(
        len(CANARY_RE.findall((folder / name).read_text(encoding="utf-8")))
        for name in manifest["files"]
    )
    out = HiddenCheck(canaries=canaries)
    sigs = signatures((row.text() for row in rows), near)
    for world in config["worlds"]:
        hidden = hidden_rows(
            world, recent=config["recent_steps"], max_chars=config["max_response_chars"]
        )
        hidden_sigs = signatures((row.text() for row in hidden.rows), near)
        pool = [*sigs, *hidden_sigs]
        sides = ["dataset"] * len(sigs) + ["hidden"] * len(hidden_sigs)
        found = near_pairs(pool, sides, near, queries=list(range(len(sigs), len(pool))))
        out.worlds[world] = {
            "hidden_tasks": hidden.tasks,
            "hidden_rows": len(hidden.rows),
            "near_duplicate_rows": len({min(pair.left, pair.right) for pair in found}),
        }
    return out


__all__ = ["HiddenCheck", "check_dataset", "check_hidden", "published_hashes"]
