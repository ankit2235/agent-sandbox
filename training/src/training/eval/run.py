"""Running one backend over rows: one prediction per row, with its measured latency.

Every backend is given the same :class:`guard.DecisionRequest` for a row, and nothing
else: not the label, not the source, not the family. Latency is the wall time of one
``decide`` call, the only wall-clock reading in the training code; it measures the
backend and never feeds a world.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field

from guard.decision import BackendResult, DecisionBackend, DecisionLabel

from ..data.example import ActionExample

Timer = Callable[[], float]


class Prediction(BaseModel):
    """One backend's answer for one row, with the row's label beside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    example_id: str
    world: str
    category: str
    family: str
    split: str | None = None
    stressed: bool
    label: DecisionLabel
    label_codes: list[str] = Field(default_factory=list)
    result: BackendResult
    latency_ms: float


def predict(
    backend: DecisionBackend,
    rows: Iterable[ActionExample],
    *,
    timer: Timer = time.perf_counter,  # purity-allow: backend latency, never a world's clock
) -> list[Prediction]:
    """Ask the backend about every row, in order."""
    out: list[Prediction] = []
    for row in rows:
        start = timer()
        result = backend.decide(row.request)
        elapsed = (timer() - start) * 1000.0
        out.append(
            Prediction(
                example_id=row.id,
                world=row.world,
                category=row.category,
                family=row.family,
                split=row.split,
                stressed=row.stressed,
                label=row.label.decision,
                label_codes=row.label.reason_codes,
                result=result,
                latency_ms=elapsed,
            )
        )
    return out


__all__ = ["Prediction", "Timer", "predict"]
