"""The alerts service: a SIEM-style alert queue."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import EVENT_FIRE, Alerts


class AlertsService(WorldService):
    """The alerts service one episode holds."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.alerts = Alerts(rng.child("alerts"))
        super().__init__("alerts", create_app(self.alerts), self.alerts.store, clock)


__all__ = ["EVENT_FIRE", "Alerts", "AlertsService"]
