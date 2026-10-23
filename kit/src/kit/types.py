"""Frozen core types (kit-v0.1).

Every world, grader and runner speaks these types and nothing else. Changing a
field here is a breaking change: tag a new kit version and write a migration
note.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Visibility = Literal["public", "hidden"]
Difficulty = Literal["easy", "medium", "hard"]

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-.]*$")
"""Task, world and failure-type ids: lowercase, no spaces, stable in filenames."""


class Strict(BaseModel):
    """Base model for kit types: unknown fields are an error, not a surprise."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Signoff(Strict):
    """A named human taking responsibility for a task or grader."""

    name: str
    role: str
    date: dt.date


class TaskSpec(BaseModel):
    """One unit of work given to an agent, and everything needed to reproduce it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    world: str
    category: str
    difficulty: Difficulty
    instruction: str
    initial_state: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    max_steps: int = Field(gt=0)
    grader: str
    seed: int = Field(default=0, ge=0)
    tags: list[str] = Field(default_factory=list)
    visibility: Visibility = "public"
    expected_outcome: str | None = None
    reviewer_signoff: Signoff | None = None
    stress: list[str] = Field(default_factory=list)

    @field_validator("id", "world", "version", "category")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not ID_RE.match(value):
            msg = f"{value!r} must match {ID_RE.pattern}"
            raise ValueError(msg)
        return value

    @field_validator("grader")
    @classmethod
    def _dotted_path(cls, value: str) -> str:
        if value.count(":") != 1 or not all(part.strip() for part in value.split(":")):
            msg = f"grader must be 'module.path:callable', got {value!r}"
            raise ValueError(msg)
        return value

    @property
    def run_key(self) -> str:
        """Stable identity of this task for result files: ``world/version/id``."""
        return f"{self.world}/{self.version}/{self.id}"


class ToolCall(Strict):
    """The only action type in the sandbox: the agent calls one of the world's tools."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolError(Strict):
    """A deterministic, documented refusal from a tool. Never a stack trace."""

    code: str
    message: str


class Observation(Strict):
    """Exactly what the model is shown after an action."""

    text: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    sim_time: dt.datetime
    done: bool = False


class Usage(Strict):
    """Model usage for one step. Filled by the runner, never by a world."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class StepResult(Strict):
    """What :meth:`kit.world.Episode.step` returns."""

    observation: Observation
    reward: float = 0.0
    done: bool = False
    info: dict[str, Any] = Field(default_factory=dict)


def _registered_failure(value: str | None) -> str | None:
    if value is None:
        return value
    from kit.failures import failure_types  # local import: registry imports this module

    if value not in failure_types:
        msg = (
            f"failure_type {value!r} is not registered; register it on the world's "
            "WorldSpec.failure_types"
        )
        raise ValueError(msg)
    return value


class CheckResult(Strict):
    """One named assertion a grader made about the final state."""

    name: str
    passed: bool
    detail: str = ""
    failure_type: str | None = None
    """The mistake this check found, when it failed; lets the scorer see every failure."""

    @field_validator("failure_type")
    @classmethod
    def _registered(cls, value: str | None) -> str | None:
        return _registered_failure(value)


class GradeResult(Strict):
    """The verdict on one episode."""

    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    checks: list[CheckResult] = Field(default_factory=list)
    failure_type: str | None = None

    @field_validator("failure_type")
    @classmethod
    def _registered(cls, value: str | None) -> str | None:
        return _registered_failure(value)

    @property
    def failed_checks(self) -> list[CheckResult]:
        """The checks that did not pass, for a one-line failure summary."""
        return [check for check in self.checks if not check.passed]


class FailureTypeDef(Strict):
    """A mistake that matters in some domain, declared by the world that can make it."""

    id: str
    label: str
    description: str
    high_impact: bool = False

    @field_validator("id")
    @classmethod
    def _namespaced(cls, value: str) -> str:
        if not ID_RE.match(value) or value.count(".") != 1:
            msg = f"failure type id must be '<namespace>.<name>', got {value!r}"
            raise ValueError(msg)
        return value

    @property
    def namespace(self) -> str:
        """``kit`` for the generic types, otherwise the world name."""
        return self.id.split(".", 1)[0]
