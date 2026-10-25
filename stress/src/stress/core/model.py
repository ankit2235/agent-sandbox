"""What a world hands the stress library: its modifiers, each with its scripted runs.

A modifier is a pure, seeded function ``(task, seed) -> TaskSpec`` (decision 102). It
returns the task with the id ``<id>.<name>`` and its name appended to
``TaskSpec.stress``; the grader path never changes, because everything the grader
must now expect travels inside the task's ``initial_state``.

Every task needs a golden run and a wrong run (CONTRIBUTING rule 4), stressed ones
included, so a modifier also says how a base task's golden run changes under it
(``adjust``) and how an agent that takes its bait behaves (``trap``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kit.scripted import Run, Step
from kit.types import TaskSpec

from .catalogue import GENERIC

Apply = Callable[[TaskSpec, int], TaskSpec]
Adjust = Callable[[TaskSpec, list[Step]], list[Step]]
"""(stressed task, golden steps so far) -> golden steps under this modifier."""
Trap = Callable[[TaskSpec, list[Step]], Run]
"""(stressed task, adjusted golden steps) -> a run that takes the bait, with ``fails_with``."""


class StressError(ValueError):
    """A modifier cannot be applied as asked."""


class NotApplicable(StressError):
    """The modifier has nothing to work on in this task, or would make it unsolvable."""


def keep(task: TaskSpec, steps: list[Step]) -> list[Step]:
    """The golden run needs no change under this modifier."""
    return steps


@dataclass(frozen=True, slots=True)
class Modifier:
    """One modifier, as a world implements it."""

    name: str
    apply: Apply
    summary: str
    trap: Trap
    adjust: Adjust = keep
    extra_steps: int = 0
    """Tool calls the stress adds to a correct run; :func:`stress.core.apply` raises
    ``max_steps`` by this much, so a careful agent is not cut off for being careful."""

    @property
    def generic(self) -> bool:
        """Whether this implements one of the generic contracts."""
        return self.name in GENERIC


@dataclass(frozen=True, slots=True)
class WorldStress:
    """A world's modifiers, generic and its own, and the aliases it answers to."""

    world: str
    modifiers: Mapping[str, Modifier]
    aliases: Mapping[str, str] = field(default_factory=dict)
    """Other names for a modifier, e.g. the build prompt's ``flaky_rails``."""

    def __post_init__(self) -> None:
        for name, modifier in self.modifiers.items():
            if name != modifier.name:
                msg = f"{self.world}: modifier registered as {name!r} is named {modifier.name!r}"
                raise ValueError(msg)
        for alias, target in self.aliases.items():
            if alias in self.modifiers:
                msg = f"{self.world}: alias {alias!r} hides a modifier of that name"
                raise ValueError(msg)
            if target not in self.modifiers:
                msg = f"{self.world}: alias {alias!r} points at unknown modifier {target!r}"
                raise ValueError(msg)

    def resolve(self, name: str) -> Modifier:
        """The modifier a name or alias stands for."""
        found = self.modifiers.get(self.aliases.get(name, name))
        if found is None:
            known = sorted([*self.modifiers, *self.aliases])
            msg = f"unknown {self.world} modifier {name!r}; have {known}"
            raise KeyError(msg)
        return found

    def names(self) -> list[str]:
        """The modifiers' own names (no aliases), sorted."""
        return sorted(self.modifiers)


def derive(task: TaskSpec, name: str, initial_state: dict[str, Any]) -> TaskSpec:
    """The task with a new ``initial_state``, the derived id and ``name`` in ``stress``."""
    if name in task.stress:
        msg = f"{task.id}: {name} is already applied"
        raise NotApplicable(msg)
    return task.model_copy(
        update={
            "id": f"{task.id}.{name}",
            "initial_state": initial_state,
            "stress": [*task.stress, name],
        }
    )


def without_expect(steps: Iterable[Step]) -> list[Step]:
    """Steps with their ``expect`` lines dropped: those describe the unstressed world."""
    return [step.model_copy(update={"expect": None}) for step in steps]


def before_end(steps: Sequence[Step], extra: Sequence[Step], terminal: Iterable[str]) -> list[Step]:
    """``extra`` inserted at the end, but before a closing terminal call (such as ``ask_user``)."""
    out = list(steps)
    at = len(out) - 1 if out and out[-1].tool in set(terminal) else len(out)
    return [*out[:at], *extra, *out[at:]]


def step(tool: str, save: str | None = None, **args: Any) -> Step:
    """One scripted step."""
    return Step(tool=tool, args=args, save=save)


__all__ = [
    "Adjust",
    "Apply",
    "Modifier",
    "NotApplicable",
    "StressError",
    "Trap",
    "WorldStress",
    "before_end",
    "derive",
    "keep",
    "step",
    "without_expect",
]
