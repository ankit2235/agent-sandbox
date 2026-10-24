"""``apply(task, [modifiers], seed)``, the stressed task set, and stressed tasks' runs.

A world registers its :class:`~stress.core.model.WorldStress` through the
``agent_sandbox.stress`` entry-point group, under its own name::

    [project.entry-points."agent_sandbox.stress"]
    payments = "worlds.payments.stress:STRESS"

so this module finds a world's modifiers without importing the world.
"""

from __future__ import annotations

import importlib.metadata as metadata
from collections.abc import Iterable, Sequence
from pathlib import Path

from kit.rng import SeededRNG
from kit.scripted import GOLDEN, WRONG_PREFIX, Run, RunFile, load_runs
from kit.types import TaskSpec
from kit.world import worlds

from .model import Modifier, NotApplicable, StressError, WorldStress, without_expect

ENTRY_POINT_GROUP = "agent_sandbox.stress"
SEED_RANGE = 2**31


class StressRegistry:
    """Every installed world's modifiers, found through entry points."""

    def __init__(self) -> None:
        self._worlds: dict[str, WorldStress] = {}
        self._discovered = False

    def register(self, stress: WorldStress) -> WorldStress:
        """Add a world's modifiers directly (tests do)."""
        self._worlds[stress.world] = stress
        return stress

    def discover(self) -> None:
        """Load every installed world's modifiers once."""
        if self._discovered:
            return
        for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
            loaded = entry.load()
            if not isinstance(loaded, WorldStress):
                msg = f"entry point {entry.name!r} must provide a WorldStress, got {type(loaded)!r}"
                raise TypeError(msg)
            if loaded.world != entry.name:
                msg = f"entry point {entry.name!r} provides {loaded.world!r}; names must match"
                raise ValueError(msg)
            self._worlds.setdefault(loaded.world, loaded)
        self._discovered = True

    def get(self, world: str) -> WorldStress:
        """One world's modifiers."""
        self.discover()
        try:
            return self._worlds[world]
        except KeyError:
            msg = f"world {world!r} has no stress modifiers; installed: {sorted(self._worlds)}"
            raise KeyError(msg) from None

    def names(self) -> list[str]:
        """Worlds with modifiers."""
        self.discover()
        return sorted(self._worlds)


registry = StressRegistry()


def child_seed(seed: int, name: str) -> int:
    """The seed a modifier gets inside :func:`apply`: derived from ``seed`` and its name."""
    return SeededRNG(seed).child(name).randint(0, SEED_RANGE)


UNCHANGED = ("world", "version", "category", "difficulty", "instruction", "grader", "visibility")
"""Fields no modifier may touch: the user's instruction and the grader stay the truth."""


def _checked(before: TaskSpec, after: TaskSpec, modifier: Modifier) -> TaskSpec:
    changed = [name for name in UNCHANGED if getattr(before, name) != getattr(after, name)]
    if changed:
        msg = f"{modifier.name} changed {', '.join(changed)} of {before.id}"
        raise StressError(msg)
    if after.id != f"{before.id}.{modifier.name}" or after.stress != [
        *before.stress,
        modifier.name,
    ]:
        msg = f"{modifier.name} must derive id {before.id}.{modifier.name} and record itself"
        raise StressError(msg)
    return after.model_copy(update={"max_steps": after.max_steps + modifier.extra_steps})


def apply(task: TaskSpec, names: Sequence[str], seed: int) -> TaskSpec:
    """Apply modifiers in order, each seeded from ``seed`` and its own name.

    Raises ``KeyError`` for an unknown name, :class:`NotApplicable` when a modifier has
    nothing to change (or a combination conflicts), and :class:`StressError` for a
    modifier asked for twice.
    """
    stress = registry.get(task.world)
    resolved = [stress.resolve(name) for name in names]
    repeated = sorted({m.name for m in resolved if [r.name for r in resolved].count(m.name) > 1})
    if repeated:
        msg = f"modifiers asked for more than once: {repeated}"
        raise StressError(msg)
    spec = worlds.get(task.world)
    for modifier in resolved:
        if modifier.name in task.stress:
            msg = f"{task.id}: {modifier.name} is already applied"
            raise StressError(msg)
        task = _checked(task, modifier.apply(task, child_seed(seed, modifier.name)), modifier)
        spec.validate_task(task)
    return task


def _base_runs(task: TaskSpec, runs_dir: Path | None) -> RunFile | None:
    return None if runs_dir is None else load_runs(task.id, runs_dir)


def _stressed(task: TaskSpec, name: str, seed: int, base: RunFile | None) -> TaskSpec | None:
    """``task`` under ``name``, or None if it does not apply (or, given the base task's runs,
    if the stressed task would have no trap run: the stress gives nothing to fall for).
    """
    try:
        stressed = apply(task, [name], seed)
        if base is not None:
            scripted_runs(stressed, base)
    except NotApplicable:
        return None
    return stressed


def applicable(task: TaskSpec, seed: int, runs_dir: Path | None = None) -> dict[str, TaskSpec]:
    """Every modifier of the task's world that applies to it, with the stressed task."""
    found: dict[str, TaskSpec] = {}
    base = _base_runs(task, runs_dir)
    for name in registry.get(task.world).names():
        stressed = _stressed(task, name, seed, base)
        if stressed is not None:
            found[name] = stressed
    return found


def task_seed(seed: int, task: TaskSpec) -> int:
    """The seed one task is stressed with inside a set."""
    return child_seed(seed, f"task/{task.id}")


def stressed_set(
    tasks: Iterable[TaskSpec], seed: int = 0, runs_dir: Path | None = None
) -> list[TaskSpec]:
    """One applicable modifier per task, spread evenly over the modifiers.

    Tasks go in id order; each takes the applicable modifier used least so far, ties
    broken by a seeded order. A task no modifier applies to is left out; with the
    world's ``runs_dir``, so is a pairing whose stressed task would have no trap run.
    """
    used: dict[str, int] = {}
    out: list[TaskSpec] = []
    for task in sorted(tasks, key=lambda t: t.id):
        options = applicable(task, task_seed(seed, task), runs_dir)
        if not options:
            continue
        order = sorted(options)
        SeededRNG(seed).child(f"order/{task.id}").shuffle(order)
        name = min(order, key=lambda n: used.get(n, 0))
        used[name] = used.get(name, 0) + 1
        out.append(options[name])
    return out


def per_modifier(
    tasks: Iterable[TaskSpec],
    names: Sequence[str],
    count: int,
    seed: int = 0,
    runs_dir: Path | None = None,
) -> dict[str, list[TaskSpec]]:
    """Up to ``count`` stressed tasks per modifier, from the tasks it applies to (seeded)."""
    pool = sorted(tasks, key=lambda t: t.id)
    bases = {task.id: _base_runs(task, runs_dir) for task in pool}
    out: dict[str, list[TaskSpec]] = {}
    for name in names:
        stressed: list[TaskSpec] = []
        for task in pool:
            found = _stressed(task, name, task_seed(seed, task), bases[task.id])
            if found is not None:
                stressed.append(found)
        if len(stressed) > count:
            stressed = SeededRNG(seed).child(f"sample/{name}").sample(stressed, count)
        out[name] = sorted(stressed, key=lambda t: t.id)
    return out


def base_id(task: TaskSpec) -> str:
    """The id of the task a stressed task was derived from."""
    suffix = "".join(f".{name}" for name in task.stress)
    if not task.stress or not task.id.endswith(suffix):
        msg = f"{task.id} is not a derived stressed task"
        raise ValueError(msg)
    return task.id[: -len(suffix)]


def scripted_runs(task: TaskSpec, base: RunFile, *, task_id: str | None = None) -> RunFile:
    """A stressed task's golden run and one wrong run per modifier, from its base's runs.

    The golden run is the base golden run (without its ``expect`` lines) adjusted by each
    modifier in order. Each modifier's trap takes its bait on top of that; a modifier
    with no bait in this task (``NotApplicable``) gets no wrong run, but at least one
    modifier must have one.
    """
    stress = registry.get(task.world)
    modifiers = [stress.resolve(name) for name in task.stress]
    steps = without_expect(base.runs[GOLDEN].steps)
    for modifier in modifiers:
        steps = modifier.adjust(task, steps)
    runs: dict[str, Run] = {GOLDEN: Run(steps=steps)}
    reasons: list[str] = []
    for modifier in modifiers:
        try:
            trap = modifier.trap(task, steps)
        except NotApplicable as error:
            reasons.append(str(error))
            continue
        if trap.fails_with is None:
            msg = f"{modifier.name}: a trap run must say what it fails with"
            raise StressError(msg)
        runs[f"{WRONG_PREFIX}{modifier.name}"] = trap
    if len(runs) == 1:
        msg = f"{task.id}: no modifier has a bait to take here ({'; '.join(reasons)})"
        raise NotApplicable(msg)
    return RunFile(task=task_id or task.id, runs=runs)


__all__ = [
    "ENTRY_POINT_GROUP",
    "StressRegistry",
    "applicable",
    "apply",
    "base_id",
    "child_seed",
    "per_modifier",
    "registry",
    "scripted_runs",
    "stressed_set",
    "task_seed",
]
