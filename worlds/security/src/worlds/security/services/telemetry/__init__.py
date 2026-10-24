"""The telemetry service: a synthetic log store with a query API."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import EVENT_BEAT, Telemetry


class TelemetryService(WorldService):
    """The telemetry service one episode holds."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.telemetry = Telemetry(rng.child("telemetry"))
        super().__init__("telemetry", create_app(self.telemetry), self.telemetry.store, clock)


__all__ = ["EVENT_BEAT", "Telemetry", "TelemetryService"]
