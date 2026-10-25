"""Rows from the hidden set, in memory only, through the runner's checked loader.

The hidden set is read only as :func:`runner.hidden.load_hidden` reads it: from
``HIDDEN_TASKS_DIR``, every task hidden with one valid canary, and the whole set
matching its published hashes. Its runs come from the same private repository. Each
hidden task's base public task (its family) is read from the set's own ``set.json``
there, after those checks, so hidden results can be reported by whether the family was
trained on.

Nothing here writes a file, and nothing it raises names a hidden task: a failed check
is reported as a failed check, with ``sandbox hidden check`` as the place to look.
Callers may keep only aggregate counts and scores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from guard.actions import actions
from guard.requests import MAX_RESPONSE_CHARS, RECENT_STEPS
from kit.scripted import GOLDEN, load_runs
from kit.world import worlds
from runner.hidden import (
    SET_FILE,
    HiddenSetError,
    content_hash,
    load_hidden,
    private_root,
    runs_dir,
    tasks_dir,
)

from .. import DATASET_VERSION
from .example import ActionExample, RunKind, Source, example_id
from .labels import golden_asks, label, walk


class HiddenRowsError(RuntimeError):
    """The hidden set could not be used; the message never names a hidden task."""


@dataclass(slots=True)
class HiddenRows:
    """The hidden set's rows, and counts about them."""

    rows: list[ActionExample] = field(default_factory=list)
    tasks: int = 0
    runs: int = 0
    misbehaved: int = 0


def hidden_rows(
    world: str,
    *,
    recent: int = RECENT_STEPS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> HiddenRows:
    """Every labelled attempt in one world's hidden set."""
    spec = worlds.get(world)
    world_actions = actions.get(world)
    try:
        tasks = load_hidden(spec)
        root = private_root()
    except HiddenSetError:
        msg = (
            f"the {world} hidden set is missing or failed its checks; "
            f"run `sandbox hidden check --world {world}` for details"
        )
        raise HiddenRowsError(msg) from None
    made = json.loads((tasks_dir(spec, root) / SET_FILE).read_text(encoding="utf-8"))["tasks"]
    out = HiddenRows(tasks=len(tasks))
    for task in tasks:
        try:
            runs = load_runs(task.id, runs_dir(spec, root))
            family = str(made[task.id]["base"])
        except (FileNotFoundError, KeyError):
            msg = f"the {world} hidden set lacks a run file or a set.json entry for a task"
            raise HiddenRowsError(msg) from None
        asks = golden_asks(dict(runs.runs), world_actions)
        task_hash = content_hash(task)
        for name, run in runs.runs.items():
            walked = walk(spec, task, run, world_actions, recent=recent, max_chars=max_chars)
            out.runs += 1
            if not walked.behaved:
                out.misbehaved += 1
                continue
            kind: RunKind = "golden" if name == GOLDEN else "wrong"
            for attempt in walked.attempts:
                decided = label(attempt, world_actions, golden_asks=asks)
                if kind == "golden" and decided.decision != "allow":
                    continue
                row = ActionExample(
                    id="pending",
                    dataset=f"{DATASET_VERSION}-hidden",
                    world=world,
                    world_version=spec.version,
                    family=family,
                    category=task.category,
                    difficulty=task.difficulty,
                    source=Source(
                        task_id=task.id,
                        task_hash=task_hash,
                        base_task=family,
                        run=name,
                        run_kind=kind,
                        step=attempt.step,
                        stress=list(task.stress),
                    ),
                    request=attempt.request,
                    label=decided,
                )
                out.rows.append(row.model_copy(update={"id": example_id(row.text(), decided)}))
    return out


__all__ = ["HiddenRows", "HiddenRowsError", "hidden_rows"]
