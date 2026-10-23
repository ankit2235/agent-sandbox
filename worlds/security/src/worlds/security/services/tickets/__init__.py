"""The tickets service: incidents, escalation with SLA timers, reports."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import EVENT_SLA_DUE, Tickets


class TicketsService(WorldService):
    """The tickets service one episode holds."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.tickets = Tickets(rng.child("tickets"))
        super().__init__("tickets", create_app(self.tickets), self.tickets.store, clock)


__all__ = ["EVENT_SLA_DUE", "Tickets", "TicketsService"]
