"""Decision models that speak the System One API: TypeSafe Jev, Cloudflare Clef, Strands Decider.

Checked against each provider's documentation on 2026-10-25:

- **Jev**: ``POST https://api.typesafe.ai/v1/systemone``, ``Authorization: Bearer`` key,
  model ``jev-latest``; the response reports the exact version (e.g. ``jev-1.13.0``).
- **Clef**: Cloudflare Workers AI, ``POST https://api.cloudflare.com/client/v4/accounts/
  <account>/ai/run/@cf/cloudflare/clef`` (or ``clef-flash``) with a bearer token; the
  body is System One's with ``model`` ``clef`` or ``clef-flash``. The page shows no
  response example, so a Cloudflare ``{"result": ...}`` envelope is accepted as well as
  a bare System One response (unverified).
- **Strands Decider**: self-hosted with ``strands-decider serve <model> --port 8000``
  (``strands-decider==0.2.0``), ``POST http://127.0.0.1:8000/v1/systemone``, no key.

The request is the rendered request as ``state``, one ``choice`` question for the
decision (the same options for every backend, :mod:`.questions`) and one ``noul``
question per candidate reason code. Reason codes with probability at least
``reason_threshold`` are kept when the decision is not ``allow``. Confidence is the
provider's own ``confidence`` for the choice, else its probability.

A timeout, an HTTP error, a response without a valid decision fails closed.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from guard.actions import ActionsRegistry, actions
from guard.decision import (
    DECISIONS,
    BackendResult,
    Decision,
    DecisionLabel,
    DecisionRequest,
    fail_closed,
)
from guard.render import render

from .questions import DECISION_OPTIONS, DECISION_QUESTION, reason_question

DECISION_ID = "decision"
MAX_QUESTIONS = 64
"""Clef's documented limit per request; the smallest of the three."""


def token_count(usage: Any, key: str) -> int | None:
    """A token count from a provider's usage object, if it gives one."""
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) else None


class ConfigError(ValueError):
    """A backend is missing something it needs to run (a key, an account id)."""


def secret(variable: str | None) -> str | None:
    """A key from the environment, refusing to run without one when one is named."""
    if variable is None:
        return None
    value = os.environ.get(variable)
    if not value:
        msg = f"set {variable} to use this backend"
        raise ConfigError(msg)
    return value


class SystemOneBackend:
    """One System One endpoint."""

    def __init__(
        self,
        name: str,
        *,
        endpoint: str,
        model: str | None,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        reason_threshold: float = 0.5,
        client: httpx.Client | None = None,
        registry: ActionsRegistry = actions,
    ) -> None:
        self.name = name
        self.endpoint = endpoint
        self.model = model
        self.reason_threshold = reason_threshold
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout_s)
        self._headers = headers
        self._registry = registry

    def _codes(self, world: str) -> list[tuple[str, str]]:
        vocabulary = self._registry.get(world)
        codes = sorted(vocabulary.caused_by_action)
        reasons = vocabulary.reason_vocabulary()
        return [(code, reasons[code]) for code in codes][: MAX_QUESTIONS - 1]

    def body(self, request: DecisionRequest) -> dict[str, Any]:
        """The request body for one decision."""
        questions: dict[str, Any] = {
            DECISION_ID: {
                "type": "choice",
                "instructions": DECISION_QUESTION,
                "criteria": dict(DECISION_OPTIONS),
            }
        }
        for index, (_, description) in enumerate(self._codes(request.world)):
            questions[f"reason_{index}"] = {
                "type": "noul",
                "instructions": reason_question(description),
            }
        body: dict[str, Any] = {"state": render(request), "questions": questions}
        if self.model is not None:
            body["model"] = self.model
        return body

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Ask the endpoint once."""
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
        """A result from a System One response (bare, or inside a ``result`` envelope)."""
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            data = data["result"]
        answers = data.get("answers") if isinstance(data, dict) else None
        version = data.get("model") if isinstance(data, dict) else None
        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = token_count(usage, "input_tokens")
        output_tokens = token_count(usage, "output_tokens")
        answer = answers.get(DECISION_ID) if isinstance(answers, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or choice not in DECISIONS:
            return fail_closed(
                self.name,
                "invalid_response",
                model=self.model,
                model_version=str(version) if version is not None else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        raw = answer.get("probabilities")
        probabilities: dict[DecisionLabel, float] | None = (
            {d: float(raw[d]) for d in DECISIONS if d in raw} if isinstance(raw, dict) else None
        )
        confidence = answer.get("confidence")
        if not isinstance(confidence, int | float):
            confidence = (probabilities or {}).get(choice, 0.0)
        codes: list[str] = []
        if choice != "allow":
            for index, (code, _) in enumerate(self._codes(request.world)):
                reason = answers.get(f"reason_{index}") if isinstance(answers, dict) else None
                value = reason.get("noul") if isinstance(reason, dict) else None
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
            model_version=str(version) if version is not None else None,
            probabilities=probabilities,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


__all__ = ["DECISION_ID", "ConfigError", "SystemOneBackend", "secret", "token_count"]
