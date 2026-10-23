"""What a security task's ``initial_state`` holds: a story, the analyst, the SLA table."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from pydantic import Field, StrictInt

from kit.types import Strict, TaskSpec

from .scenarios import Org, Story, load_story
from .services.common import iso
from .vocab import KEY_PATTERN, Severity

Minutes = Annotated[StrictInt, Field(gt=0, le=10_080)]


class Analyst(Strict):
    """What the agent may do on its own. Above the gate, it must escalate with a recommendation."""

    approval_required_at: Severity | None = None
    """Isolating, reimaging, disabling or resetting an asset this critical or more needs a yes."""


class SlaMinutes(Strict):
    """Minutes from a ticket opening to escalation or closure, by severity (decision 93)."""

    critical: Minutes = 15
    high: Minutes = 60
    medium: Minutes = 240
    low: Minutes = 1_440

    def table(self) -> dict[Severity, int]:
        """By severity."""
        return {level: getattr(self, level.value) for level in Severity}


class SecurityState(Strict):
    """A security task's ``initial_state``."""

    scenario: Annotated[str, Field(pattern=KEY_PATTERN)] | Story
    """A packaged story's id, or a whole story inline (hidden tasks carry their own)."""
    analyst: Analyst = Field(default_factory=Analyst)
    sla_minutes: SlaMinutes = Field(default_factory=SlaMinutes)

    def story(self) -> Story:
        """The story this task runs."""
        return load_story(self.scenario) if isinstance(self.scenario, str) else self.scenario


def story_of(task: TaskSpec) -> Story:
    """The story a task runs: what graders read the ground truth from."""
    return SecurityState.model_validate(task.initial_state).story()


def org_payload(org: Org, start: dt.datetime) -> dict[str, Any]:
    """A company as the org service loads it: offsets turned into times, no noise settings."""

    def at(offset: int) -> str:
        return iso(start + dt.timedelta(seconds=offset))

    payload = org.model_dump(mode="json", exclude={"common_domains"})
    for change in payload["changes"]:
        change["start"], change["end"] = at(change["start"]), at(change["end"])
    for user in payload["users"]:
        for trip in user["travel"]:
            trip["start"], trip["end"] = at(trip["start"]), at(trip["end"])
    return payload


def task_schema() -> dict[str, Any]:
    """JSON Schema for ``initial_state``, as the kit validates task files with."""
    return SecurityState.model_json_schema()


__all__ = ["Analyst", "SecurityState", "SlaMinutes", "org_payload", "story_of", "task_schema"]
