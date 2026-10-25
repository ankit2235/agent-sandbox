"""Applying a fitted calibration to a backend's answers.

A calibration (fitted by ``sandbox-train calibrate`` on the validation split, never on test
or hidden rows) holds a temperature for the backend's probabilities and a threshold: a
decision whose confidence is below the threshold becomes ask_user with the extra code
``guard.low_confidence``, so a low-confidence case is never a silent allow.
:class:`CalibratedBackend` wraps any backend with one.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from guard.decision import (
    DECISIONS,
    BackendResult,
    Decision,
    DecisionBackend,
    DecisionLabel,
    DecisionRequest,
)

LOW_CONFIDENCE = "guard.low_confidence"
CAUTION: tuple[DecisionLabel, ...] = ("block", "ask_user", "allow")


class Calibration(BaseModel):
    """What was fitted, and on what."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str
    temperature: float = Field(gt=0)
    threshold: float = Field(ge=0, le=1)
    target_missed: float = Field(ge=0, le=1)
    fitted_on: str
    rows: int
    ece_before: float | None = None
    ece_after: float | None = None

    def save(self, path: Path) -> None:
        """Write the calibration file."""
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Calibration:
        """Read a calibration file."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def scaled(probabilities: dict[DecisionLabel, float], temperature: float) -> dict[str, float]:
    """``p ** (1 / T)``, renormalised; a zero probability stays zero."""
    powered = {d: probabilities.get(d, 0.0) ** (1 / temperature) for d in DECISIONS}
    total = sum(powered.values())
    if total <= 0:
        return {d: 1 / len(DECISIONS) for d in DECISIONS}
    return {d: value / total for d, value in powered.items()}


def recalibrated(result: BackendResult, temperature: float) -> tuple[DecisionLabel, float]:
    """The decision and confidence after the temperature (unchanged without probabilities)."""
    decision = result.decision
    if not result.probabilities or result.error is not None:
        return decision.decision, decision.confidence
    probabilities = scaled(result.probabilities, temperature)
    choice = max(CAUTION, key=lambda d: probabilities[d])
    return choice, probabilities[choice]


def apply(result: BackendResult, calibration: Calibration) -> BackendResult:
    """One result with the calibration applied."""
    if result.error is not None:
        return result
    choice, confidence = recalibrated(result, calibration.temperature)
    codes = list(result.decision.reason_codes) if choice != "allow" else []
    if confidence < calibration.threshold:
        choice = "ask_user"
        codes = [*codes, LOW_CONFIDENCE] if LOW_CONFIDENCE not in codes else codes
    return result.model_copy(
        update={
            "decision": Decision(decision=choice, reason_codes=codes, confidence=confidence),
            "probabilities": (
                scaled(result.probabilities, calibration.temperature)
                if result.probabilities
                else None
            ),
        }
    )


class CalibratedBackend:
    """A backend with a calibration applied to every answer."""

    def __init__(self, inner: DecisionBackend, calibration: Calibration) -> None:
        self.inner = inner
        self.calibration = calibration
        self.name = f"{inner.name}+calibrated"

    def decide(self, request: DecisionRequest) -> BackendResult:
        """The inner backend's answer, calibrated."""
        result = apply(self.inner.decide(request), self.calibration)
        return result.model_copy(update={"backend": self.name})


__all__ = [
    "CAUTION",
    "LOW_CONFIDENCE",
    "CalibratedBackend",
    "Calibration",
    "apply",
    "recalibrated",
    "scaled",
]
