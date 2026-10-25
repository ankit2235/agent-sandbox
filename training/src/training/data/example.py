"""One row of the action dataset: an attempted high-impact action and its correct decision.

The input is a :class:`guard.DecisionRequest`, the same object every backend is given.
The label comes from the world's graders (:mod:`training.data.labels`), never from a
model or a person's guess. ``source`` says exactly which task, run and step the row was
cut from, so any row can be rebuilt from the public code.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from guard.decision import DecisionLabel, DecisionRequest
from guard.render import render

Split = Literal["train", "validation", "test"]
SPLITS: tuple[Split, ...] = ("train", "validation", "test")
RunKind = Literal["golden", "wrong"]
"""Run files hold a golden run and named wrong runs. A stressed task's wrong runs are
its modifiers' baits (``wrong_<modifier>``); they are the dataset's hard cases."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Strict):
    """Where a row was cut from."""

    task_id: str
    task_hash: str
    """sha256 of the task as canonical JSON, as the hidden-set hash lists are computed."""
    base_task: str
    """The public task this task was made from (itself, if it is a public task)."""
    run: str
    run_kind: RunKind
    step: int
    """The attempt's position in the run, from 0."""
    stress: list[str] = Field(default_factory=list)
    template: str | None = None
    """The template file's base task, when the task is a template variant."""
    variant: int | None = None
    stress_seed: int | None = None


class Label(_Strict):
    """The correct decision, and why."""

    decision: DecisionLabel
    reason_codes: list[str] = Field(default_factory=list)
    """The failure types the attempt caused, then the grader checks it broke."""
    checks: list[str] = Field(default_factory=list)
    """The names of the grader checks that started failing right after the attempt."""


class ActionExample(_Strict):
    """One labelled attempt."""

    id: str
    dataset: str
    world: str
    world_version: str
    family: str
    """The group of public tasks the row belongs to; a family never spans two splits."""
    split: Split | None = None
    category: str
    difficulty: str
    source: Source
    request: DecisionRequest
    label: Label

    @property
    def stressed(self) -> bool:
        """Whether the row comes from a stressed task."""
        return bool(self.source.stress)

    def text(self) -> str:
        """The request as a model is shown it."""
        return render(self.request)


def text_key(text: str) -> str:
    """The key exact duplicates are found by."""
    return hashlib.sha256(text.encode()).hexdigest()


def example_id(text: str, label: Label) -> str:
    """A row's id: the hash of what a model sees and the label it should give."""
    payload = json.dumps([text, label.model_dump(mode="json")], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


__all__ = [
    "SPLITS",
    "ActionExample",
    "Label",
    "RunKind",
    "Source",
    "Split",
    "example_id",
    "text_key",
]
