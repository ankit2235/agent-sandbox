"""The rails as a kit ``WorldState``: mounted in-process, scheduling their own events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from kit.clock import ScheduledEvent, SimClock
from kit.rng import SeededRNG
from kit.service import AsgiService
from kit.trace import DiffEntry

from .app import create_app
from .core import Rails

EVENT_PREFIX = "rails."


class RailsService(AsgiService):
    """The rails service one episode holds.

    Snapshot and restore come from :class:`kit.service.AsgiService` over the
    rails' :class:`kit.service.SqliteStore`. Graders read it only through
    ``query``, which calls ``GET /admin/<resource>``.
    """

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.rails = Rails(rng.child("rails"))
        super().__init__("rails", create_app(self.rails), self.rails.store, clock)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Call the rails, then put any events they asked for on the episode's clock."""
        response = super().request(
            method, path, json_body=json_body, params=params, headers=headers
        )
        if method.upper() == "POST" and response.is_success:
            for raw in response.json().get("scheduled_events", []):
                self.clock.schedule(ScheduledEvent.model_validate(raw))
        return response

    def load(self, config: Mapping[str, Any]) -> None:
        """Load the ``rails`` part of a task's ``initial_state``."""
        response = self.post("/internal/load", dict(config))
        if response.status_code != 200:
            error = response.json()["error"]
            msg = f"the rails refused the task's initial state: {error['message']}"
            raise ValueError(msg)

    @staticmethod
    def handles(event: ScheduledEvent) -> bool:
        """Whether a scheduled event is one the rails apply."""
        return event.name.startswith(EVENT_PREFIX)

    def apply_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        """Deliver a due event and return the rows it changed, for the trace."""
        response = self.post("/internal/events", {"name": event.name, "payload": event.payload})
        if response.status_code != 200:
            # An event the rails refuse is a bug in the task or the world, never agent error.
            msg = f"the rails refused event {event.name!r}: {response.json()['error']['message']}"
            raise RuntimeError(msg)
        return [DiffEntry.model_validate(entry) for entry in response.json()["state_diff"]]


__all__ = ["EVENT_PREFIX", "RailsService"]
