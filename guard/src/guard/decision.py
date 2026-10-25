"""What a decision backend is asked, what it answers, and the one interface every backend has.

A :class:`DecisionRequest` is everything a backend may see about one attempted
high-impact action: the user's instruction, the delegated policy, the relevant world
state, the recent trace and what the agent read, and the action itself. It is built
the same way for every backend, so they are compared on identical inputs.

A :class:`Decision` is the output contract: ``{decision, reason_codes[], confidence}``,
checked against :data:`DECISION_SCHEMA`. A backend that times out, errors, refuses or
answers with something that is not a valid decision never allows: :func:`fail_closed`
turns that into ``ask_user`` with confidence 0 and an error code.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from importlib.resources import files
from typing import Any, Literal, Protocol, runtime_checkable

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionLabel = Literal["allow", "block", "ask_user"]
DECISIONS: tuple[DecisionLabel, ...] = ("allow", "block", "ask_user")

REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*(\.[a-z0-9][a-z0-9_\-]*)+$")
"""A reason code is dotted and namespaced: ``payments.overspend``, ``guard.timeout``."""

DECISION_SCHEMA: dict[str, Any] = json.loads(
    files("guard").joinpath("schemas/decision.schema.json").read_text(encoding="utf-8")
)
"""The strict JSON Schema of a decision: no other keys, known decisions, codes as above."""

_FENCE = re.compile(r"^```(?:json)?\s*\n(?P<body>.*)\n```$", re.DOTALL)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionCall(_Strict):
    """The action the agent is about to take: one tool call."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class StepView(_Strict):
    """One earlier step of the episode, as a backend sees it: never the agent's reasoning."""

    kind: Literal["tool_call", "event"]
    sim_time: dt.datetime
    tool: str | None = None
    event: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    response: Any = None
    error: str | None = None
    """The refusal code, if the tool refused the call."""


class DecisionRequest(_Strict):
    """Everything a backend is given about one attempted action."""

    world: str
    instruction: str
    policy: dict[str, Any] = Field(default_factory=dict)
    """What the user delegated: the tools allowed, plus the world's own facts (mandates, caps)."""
    state: dict[str, Any] = Field(default_factory=dict)
    """The world state relevant to this action, read just before it."""
    recent_steps: list[StepView] = Field(default_factory=list)
    """The last steps before the action, oldest first."""
    read: list[StepView] = Field(default_factory=list)
    """Earlier steps that only read (pages, logs, messages), oldest first."""
    action: ActionCall
    sim_time: dt.datetime


class Decision(_Strict):
    """The output contract every backend's answer is reduced to."""

    decision: DecisionLabel
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("reason_codes")
    @classmethod
    def _codes(cls, value: list[str]) -> list[str]:
        bad = [code for code in value if not REASON_RE.match(code)]
        if bad:
            msg = f"reason codes must be dotted ids like 'payments.overspend', got {bad}"
            raise ValueError(msg)
        if len(set(value)) != len(value):
            msg = f"reason codes repeat: {value}"
            raise ValueError(msg)
        return value


class BackendResult(_Strict):
    """One backend's answer to one request, with what it cost and who answered."""

    decision: Decision
    backend: str
    model: str | None = None
    """The model id the backend was configured with, e.g. ``jev-latest``."""
    model_version: str | None = None
    """The exact model version the provider reported answering, when it reports one."""
    probabilities: dict[DecisionLabel, float] | None = None
    """The backend's probability for each decision, when it gives them (used for calibration)."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    """Why the backend did not answer normally: ``timeout``, ``invalid_json``, ``refusal``, ..."""


@runtime_checkable
class DecisionBackend(Protocol):
    """Anything that can decide on an attempted action."""

    name: str

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Answer one request. Must not raise for a provider failure: fail closed instead."""
        ...


class DecisionError(ValueError):
    """A backend's answer is not a valid decision."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_decision(text: str) -> Decision:
    """A decision from a model's text: one JSON object, optionally in one code fence.

    Raises :class:`DecisionError` with code ``invalid_json`` or ``schema``.
    """
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced is not None:
        body = fenced.group("body").strip()
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as error:
        raise DecisionError("invalid_json", f"not JSON: {error}") from error
    try:
        jsonschema.validate(raw, DECISION_SCHEMA)
    except jsonschema.ValidationError as error:
        raise DecisionError("schema", f"not a decision: {error.message}") from error
    try:
        return Decision.model_validate(raw)
    except ValueError as error:
        raise DecisionError("schema", f"not a decision: {error}") from error


def fail_closed(
    backend: str,
    error: str,
    *,
    model: str | None = None,
    model_version: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> BackendResult:
    """The answer for a backend that could not decide: ask the user, never allow."""
    code = re.sub(r"[^a-z0-9_\-]", "_", error.lower()) or "error"
    return BackendResult(
        decision=Decision(decision="ask_user", reason_codes=[f"guard.{code}"], confidence=0.0),
        backend=backend,
        model=model,
        model_version=model_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=code,
    )


__all__ = [
    "DECISIONS",
    "DECISION_SCHEMA",
    "REASON_RE",
    "ActionCall",
    "BackendResult",
    "Decision",
    "DecisionBackend",
    "DecisionError",
    "DecisionLabel",
    "DecisionRequest",
    "StepView",
    "fail_closed",
    "parse_decision",
]
