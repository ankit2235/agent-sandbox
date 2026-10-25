"""Our model as a :class:`guard.DecisionBackend`, over any :class:`Scorer`.

The decision is the most probable of the three openings (:func:`.format.opening`),
normalised over the three, so the backend has a probability for each decision (what
:mod:`training.eval.calibration` calibrates). On a tie the more cautious decision wins
(block, then ask_user, then allow). The reason codes are then generated after the
chosen opening; a malformed generation fails closed.

The real scorer is :class:`guard.backends.guard_model.hf.HFScorer` (transformers, local
inference); tests use a fake one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from guard.actions import ActionsRegistry, actions
from guard.decision import (
    BackendResult,
    Decision,
    DecisionLabel,
    DecisionRequest,
    fail_closed,
)

from .format import opening, prompt, reasons_after, reasons_for

CAUTION: tuple[DecisionLabel, ...] = ("block", "ask_user", "allow")
MAX_NEW_TOKENS = 64


class Scorer(Protocol):
    """A language model that can score continuations and continue a text."""

    model_id: str
    revision: str | None

    def logprobs(self, text: str, continuations: Sequence[str]) -> list[float]:
        """The total log-probability of each continuation after ``text``."""
        ...

    def generate(self, text: str, max_new_tokens: int) -> str:
        """A greedy continuation of ``text``."""
        ...


def softmax(values: Sequence[float]) -> list[float]:
    """Probabilities from log-probabilities, stably."""
    top = max(values)
    weights = [math.exp(value - top) for value in values]
    total = sum(weights)
    return [weight / total for weight in weights]


class GuardModelBackend:
    """Our fine-tuned model."""

    def __init__(
        self,
        name: str,
        scorer: Scorer,
        *,
        registry: ActionsRegistry = actions,
    ) -> None:
        self.name = name
        self.scorer = scorer
        self._registry = registry

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Score the three openings, then generate the reason codes."""
        text = prompt(request, reasons_for(self._registry.get(request.world)))
        probabilities = dict(
            zip(
                CAUTION,
                softmax(self.scorer.logprobs(text, [opening(d) for d in CAUTION])),
                strict=True,
            )
        )
        choice = max(CAUTION, key=lambda d: probabilities[d])
        model, version = self.scorer.model_id, self.scorer.revision
        codes = reasons_after(choice, self.scorer.generate(text + opening(choice), MAX_NEW_TOKENS))
        if codes is None:
            return fail_closed(self.name, "invalid_json", model=model, model_version=version)
        try:
            decision = Decision(
                decision=choice,
                reason_codes=codes if choice != "allow" else [],
                confidence=probabilities[choice],
            )
        except ValueError:
            return fail_closed(self.name, "schema", model=model, model_version=version)
        return BackendResult(
            decision=decision,
            backend=self.name,
            model=model,
            model_version=version,
            probabilities=probabilities,
        )


__all__ = ["CAUTION", "MAX_NEW_TOKENS", "GuardModelBackend", "Scorer", "softmax"]
