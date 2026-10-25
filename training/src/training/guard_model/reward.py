"""The reward for Stage B (optional RL), from the dataset's grader-derived labels.

A completion is scored against the row's correct decision, with wrongful blocks and
missed mistakes penalised separately (the build prompt asks for both):

- right decision: ``correct``, plus ``reason_bonus`` x the Jaccard overlap of the reason
  codes with the label's;
- the label allows, the completion blocks: ``-wrongful_block``;
- the label allows, the completion asks: ``-needless_ask``;
- the label does not allow, the completion allows: ``-missed_mistake``;
- block for ask_user or the reverse: ``-wrong_kind``;
- not a ``{"decision", "reason_codes"}`` object: ``-invalid``.

Standard library and pydantic only, so the GRPO script can import it on a GPU machine
with nothing of the sandbox installed but this package (decision 187).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DECISIONS = ("allow", "block", "ask_user")
"""The same three as ``guard.DECISIONS`` (a test keeps them equal)."""


class RewardWeights(BaseModel):
    """How much each outcome is worth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correct: float = Field(default=1.0, ge=0)
    reason_bonus: float = Field(default=0.2, ge=0)
    wrongful_block: float = Field(default=1.0, ge=0)
    needless_ask: float = Field(default=0.3, ge=0)
    missed_mistake: float = Field(default=2.0, ge=0)
    wrong_kind: float = Field(default=0.5, ge=0)
    invalid: float = Field(default=1.0, ge=0)


def parse_completion(text: str) -> tuple[str, list[str]] | None:
    """``(decision, reason codes)`` from a generated completion, or None if it is not one."""
    body = text.strip()
    end = body.find("}")
    if not body.startswith("{") or end < 0:
        return None
    try:
        parsed: Any = json.loads(body[: end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    decision, codes = parsed.get("decision"), parsed.get("reason_codes")
    if decision not in DECISIONS or not isinstance(codes, list):
        return None
    if not all(isinstance(code, str) for code in codes):
        return None
    return str(decision), list(dict.fromkeys(codes))


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    left, right = set(a), set(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def reward(
    completion: str, decision: str, reason_codes: Sequence[str], weights: RewardWeights
) -> float:
    """The reward for one completion against one label."""
    parsed = parse_completion(completion)
    if parsed is None:
        return -weights.invalid
    predicted, codes = parsed
    if predicted == decision:
        return weights.correct + weights.reason_bonus * _jaccard(codes, reason_codes)
    if decision == "allow":
        return -weights.wrongful_block if predicted == "block" else -weights.needless_ask
    if predicted == "allow":
        return -weights.missed_mistake
    return -weights.wrong_kind


def trl_reward(weights: RewardWeights) -> Callable[..., list[float]]:
    """A reward function in TRL's GRPOTrainer form: the label columns arrive as keywords.

    The dataset carries ``decision`` and ``reason_codes`` (a JSON list) beside ``prompt``.
    """

    def guard_reward(
        completions: list[str], decision: list[str], reason_codes: list[str], **_: Any
    ) -> list[float]:
        return [
            reward(text, label, json.loads(codes), weights)
            for text, label, codes in zip(completions, decision, reason_codes, strict=True)
        ]

    return guard_reward


__all__ = ["DECISIONS", "RewardWeights", "parse_completion", "reward", "trl_reward"]
