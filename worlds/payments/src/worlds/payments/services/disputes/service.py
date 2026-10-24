"""Disputes as a kit ``WorldState``: mounted in-process, scheduling its own events."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import Disputes


class DisputesService(WorldService):
    """The disputes service one episode holds. Graders read it through ``query``."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.disputes = Disputes(rng.child("disputes"))
        super().__init__("disputes", create_app(self.disputes), self.disputes.store, clock)


__all__ = ["DisputesService"]
