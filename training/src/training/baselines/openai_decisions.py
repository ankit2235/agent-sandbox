"""OpenAI's Decisions API as a baseline.

Checked against OpenAI's guide on 2026-10-25 (``developers.openai.com/api/docs/guides/
decisions``): generally available, ``POST https://api.openai.com/v1/decisions`` with a
bearer key, model ``gpt-6-luna`` (the only one). The body is ``model``, ``input`` (here
the rendered request) and ``questions``: a ``choice`` question whose ``choices`` carry a
``value`` and a ``description``, and ``predicate`` questions. Answers come back as a
list keyed by ``name``: a choice with ``choice``, ``probabilities`` (``value`` and
``probability`` pairs) and ``confidence``; a predicate with ``probability``; or a
``refusal``. Usage reports ``input_tokens``; output tokens are not charged.

A refusal of the decision question, a timeout, an HTTP error or a response without a
valid decision fails closed.
"""

from __future__ import annotations

from typing import Any

import httpx

from guard.decision import (
    DECISIONS,
    BackendResult,
    Decision,
    DecisionLabel,
    DecisionRequest,
    fail_closed,
)

from ..render import render
from ..world import ActionsRegistry, actions
from .questions import DECISION_OPTIONS, DECISION_QUESTION, reason_question
from .system_one import token_count

ENDPOINT = "https://api.openai.com/v1/decisions"
DECISION_NAME = "decision"


class OpenAIDecisionsBackend:
    """The Decisions API."""

    def __init__(
        self,
        name: str,
        *,
        model: str,
        api_key: str,
        endpoint: str = ENDPOINT,
        timeout_s: float = 10.0,
        reason_threshold: float = 0.5,
        client: httpx.Client | None = None,
        registry: ActionsRegistry = actions,
    ) -> None:
        self.name = name
        self.model = model
        self.endpoint = endpoint
        self.reason_threshold = reason_threshold
        self._headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        self._client = client or httpx.Client(timeout=timeout_s)
        self._registry = registry

    def _codes(self, world: str) -> list[tuple[str, str]]:
        vocabulary = self._registry.get(world)
        reasons = vocabulary.reason_vocabulary()
        return [(code, reasons[code]) for code in sorted(vocabulary.caused_by_action)]

    def body(self, request: DecisionRequest) -> dict[str, Any]:
        """The request body for one decision."""
        questions: list[dict[str, Any]] = [
            {
                "type": "choice",
                "name": DECISION_NAME,
                "instructions": DECISION_QUESTION,
                "choices": [
                    {"value": value, "description": description}
                    for value, description in DECISION_OPTIONS.items()
                ],
            }
        ]
        questions += [
            {"type": "predicate", "name": f"reason_{index}", "instructions": reason_question(text)}
            for index, (_, text) in enumerate(self._codes(request.world))
        ]
        return {"model": self.model, "input": render(request), "questions": questions}

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Ask the API once."""
        try:
            response = self._client.post(
                self.endpoint, json=self.body(request), headers=self._headers
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            return fail_closed(self.name, "timeout", model=self.model)
        except httpx.HTTPError:
            return fail_closed(self.name, "provider_error", model=self.model)
        except ValueError:
            return fail_closed(self.name, "invalid_response", model=self.model)
        return self.parse(request, data)

    def parse(self, request: DecisionRequest, data: Any) -> BackendResult:
        """A result from a Decisions API response."""
        answers = data.get("answers") if isinstance(data, dict) else None
        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = token_count(usage, "input_tokens")
        named = (
            {a["name"]: a for a in answers if isinstance(a, dict) and "name" in a}
            if isinstance(answers, list)
            else {}
        )
        answer = named.get(DECISION_NAME)
        if isinstance(answer, dict) and answer.get("type") == "refusal":
            return fail_closed(self.name, "refusal", model=self.model, input_tokens=input_tokens)
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or choice not in DECISIONS:
            return fail_closed(
                self.name, "invalid_response", model=self.model, input_tokens=input_tokens
            )
        found = {
            pair.get("value"): pair.get("probability")
            for pair in answer.get("probabilities") or []
            if isinstance(pair, dict)
        }
        probabilities: dict[DecisionLabel, float] = {}
        for d in DECISIONS:
            value = found.get(d)
            if isinstance(value, int | float):
                probabilities[d] = float(value)
        confidence = answer.get("confidence")
        if not isinstance(confidence, int | float):
            confidence = probabilities.get(choice, 0.0)
        codes: list[str] = []
        if choice != "allow":
            for index, (code, _) in enumerate(self._codes(request.world)):
                reason = named.get(f"reason_{index}")
                value = reason.get("probability") if isinstance(reason, dict) else None
                if isinstance(value, int | float) and value >= self.reason_threshold:
                    codes.append(code)
        return BackendResult(
            decision=Decision(
                decision=choice,
                reason_codes=codes,
                confidence=min(max(float(confidence), 0.0), 1.0),
            ),
            backend=self.name,
            model=self.model,
            model_version=str(data.get("model") or self.model),
            probabilities=probabilities or None,
            input_tokens=input_tokens,
        )


__all__ = ["DECISION_NAME", "ENDPOINT", "OpenAIDecisionsBackend"]
