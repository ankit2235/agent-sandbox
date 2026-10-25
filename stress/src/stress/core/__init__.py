"""Generic, seeded stress modifiers and the machinery that applies any world's.

- :mod:`.catalogue`: the generic modifiers' contracts;
- :mod:`.wording`: seeded wording worlds share;
- :mod:`.model`: :class:`Modifier` and :class:`WorldStress`, what a world registers;
- :mod:`.apply`: :func:`apply`, the stressed task set, and stressed tasks' runs.

Domain-neutral: nothing here imports a world (hard rule 8).
"""

from .apply import (
    ENTRY_POINT_GROUP,
    applicable,
    apply,
    base_id,
    per_modifier,
    registry,
    scripted_runs,
    stressed_set,
)
from .catalogue import GENERIC, Contract
from .model import (
    Modifier,
    NotApplicable,
    StressError,
    WorldStress,
    before_end,
    derive,
    keep,
    step,
    without_expect,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "GENERIC",
    "Contract",
    "Modifier",
    "NotApplicable",
    "StressError",
    "WorldStress",
    "applicable",
    "apply",
    "base_id",
    "before_end",
    "derive",
    "keep",
    "per_modifier",
    "registry",
    "scripted_runs",
    "step",
    "stressed_set",
    "without_expect",
]
