"""The response service: containment actions, their impact, and local evidence."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import Response, impact_of


class ResponseService(WorldService):
    """The response service one episode holds."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.response = Response(rng.child("response"))
        super().__init__("response", create_app(self.response), self.response.store, clock)


__all__ = ["Response", "ResponseService", "impact_of"]
