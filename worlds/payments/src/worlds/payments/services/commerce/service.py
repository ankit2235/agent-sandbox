"""Commerce as a kit ``WorldState``: mounted in-process, scheduling its own events."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import Commerce


class CommerceService(WorldService):
    """The commerce service one episode holds. Graders read it through ``query``."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.commerce = Commerce(rng.child("commerce"))
        super().__init__("commerce", create_app(self.commerce), self.commerce.store, clock)


__all__ = ["CommerceService"]
