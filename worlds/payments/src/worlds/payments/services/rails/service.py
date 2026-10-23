"""The rails as a kit ``WorldState``: mounted in-process, scheduling their own events."""

from __future__ import annotations

from kit.clock import ScheduledEvent, SimClock
from kit.rng import SeededRNG
from kit.trace import DiffEntry

from ..common import WorldService, diff_of
from .app import create_app
from .core import Rails

EVENT_PREFIX = "rails."


class RailsService(WorldService):
    """The rails service one episode holds.

    Snapshot and restore come from :class:`kit.service.AsgiService` over the
    rails' :class:`kit.service.SqliteStore`. Graders read it only through
    ``query``, which calls ``GET /admin/<resource>``.
    """

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.rails = Rails(rng.child("rails"))
        super().__init__("rails", create_app(self.rails), self.rails.store, clock)

    def apply_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        """Deliver a due event and return the rows it changed, for the trace."""
        return diff_of(self.deliver(event))


__all__ = ["EVENT_PREFIX", "RailsService"]
