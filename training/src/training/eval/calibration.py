"""Calibrating a backend's confidence, and turning low confidence into ask_user.

Fitted on the validation split's predictions, never on test or hidden rows:

1. **Temperature**: for a backend that gives a probability per decision, the one
   temperature ``T`` (grid 0.25 to 5) that minimises the negative log-likelihood of the
   labels under ``p ** (1 / T)``, renormalised. A backend without probabilities
   (rules only) keeps ``T = 1``.
2. **Threshold**: the smallest ``tau`` such that, once every decision with confidence
   below ``tau`` becomes ask_user, the missed-mistake rate on validation is at most
   ``target_missed``. If no ``tau`` reaches it, ``tau`` is 1 and every decision below
   full confidence goes to the user.

:class:`guard.backends.calibration.CalibratedBackend` applies both. A decision below the
threshold becomes ask_user with the extra code ``guard.low_confidence``: a low-confidence
case is never a silent allow. The calibration file records the ECE before and after the temperature
(the threshold's ask_user decisions are counted in neither).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from guard.backends.calibration import Calibration, apply, recalibrated, scaled

from .metrics import ece
from .run import Prediction

TEMPERATURES = [round(0.25 + 0.05 * step, 2) for step in range(96)]


def _nll(predictions: Sequence[Prediction], temperature: float) -> float:
    total = 0.0
    for p in predictions:
        assert p.result.probabilities is not None
        probability = scaled(p.result.probabilities, temperature)[p.label]
        total -= math.log(max(probability, 1e-12))
    return total


def fit_temperature(predictions: Sequence[Prediction]) -> float:
    """The best temperature, or 1 if the backend gives no probabilities."""
    usable = [p for p in predictions if p.result.probabilities and p.result.error is None]
    if not usable:
        return 1.0
    return min(TEMPERATURES, key=lambda t: (_nll(usable, t), abs(t - 1)))


def choose_threshold(
    predictions: Sequence[Prediction], temperature: float, target_missed: float
) -> float:
    """The smallest threshold that keeps validation's missed-mistake rate at the target."""
    scored = [(recalibrated(p.result, temperature), p.label) for p in predictions]
    mistakes = [(choice, confidence) for (choice, confidence), label in scored if label != "allow"]
    if not mistakes:
        return 0.0
    candidates = sorted({0.0, *(confidence for (_, confidence), _ in scored), 1.0})
    for tau in candidates:
        missed = sum(choice == "allow" and confidence >= tau for choice, confidence in mistakes)
        if missed / len(mistakes) <= target_missed:
            return tau
    return 1.0


def fit(
    backend: str, predictions: Sequence[Prediction], *, target_missed: float, fitted_on: str
) -> Calibration:
    """Fit a calibration on validation predictions."""
    if not predictions:
        msg = "cannot calibrate on no predictions"
        raise ValueError(msg)
    temperature = fit_temperature(predictions)
    threshold = choose_threshold(predictions, temperature, target_missed)
    calibration = Calibration(
        backend=backend,
        temperature=temperature,
        threshold=threshold,
        target_missed=target_missed,
        fitted_on=fitted_on,
        rows=len(predictions),
        ece_before=ece(predictions),
    )
    scaled_only = calibration.model_copy(update={"threshold": 0.0})
    after = [p.model_copy(update={"result": apply(p.result, scaled_only)}) for p in predictions]
    return calibration.model_copy(update={"ece_after": ece(after)})


__all__ = ["TEMPERATURES", "choose_threshold", "fit", "fit_temperature"]
