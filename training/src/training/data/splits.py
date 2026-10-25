"""Splits by task family, never by row.

A row's family starts as the public task it was made from. Families whose rows are
near-duplicates of each other (two tasks on one security story, say) are merged into
one group first, so near-identical rows can never land on both sides of a split; a
group is named after its smallest family id. Groups are then split per world and
category with a seeded shuffle: about ``test`` of them to the test split, ``validation``
to validation, the rest to train, and at least one group in each held-out split when a
category has three or more.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from kit.rng import SeededRNG

from .example import Split


class SplitConfig(BaseModel):
    """The share of family groups each held-out split gets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    validation: float = Field(default=0.15, ge=0.0, lt=0.5)
    test: float = Field(default=0.15, gt=0.0, lt=0.5)


class Families:
    """Union-find over family ids."""

    def __init__(self, names: Iterable[str]) -> None:
        self._parent = {name: name for name in names}

    def find(self, name: str) -> str:
        """The group a family is in, named after its smallest member."""
        root = name
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[name] != root:
            self._parent[name], name = root, self._parent[name]
        return root

    def union(self, a: str, b: str) -> bool:
        """Merge two families' groups; False if they were one already."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        low, high = sorted((ra, rb))
        self._parent[high] = low
        return True

    def groups(self) -> dict[str, list[str]]:
        """Group name -> its families, sorted."""
        out: dict[str, list[str]] = defaultdict(list)
        for name in sorted(self._parent):
            out[self.find(name)].append(name)
        return dict(out)


def assign(
    groups: Mapping[str, tuple[str, str]], seed: int, config: SplitConfig
) -> dict[str, Split]:
    """A split for each group; ``groups`` maps a group to its ``(world, category)``."""
    by_cell: dict[tuple[str, str], list[str]] = defaultdict(list)
    for group, cell in groups.items():
        by_cell[cell].append(group)
    out: dict[str, Split] = {}
    for (world, category), members in sorted(by_cell.items()):
        order = sorted(members)
        SeededRNG(seed).child(f"split/{world}/{category}").shuffle(order)
        count = len(order)
        test = round(count * config.test)
        validation = round(count * config.validation)
        if count >= 3:
            test = max(test, 1)
            validation = max(validation, 1) if config.validation > 0 else 0
        for index, group in enumerate(order):
            if index < test:
                out[group] = "test"
            elif index < test + validation:
                out[group] = "validation"
            else:
                out[group] = "train"
    return out


__all__ = ["Families", "SplitConfig", "assign"]
