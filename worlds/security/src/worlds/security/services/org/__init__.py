"""The company directory service: hosts, accounts, changes, jobs, suppliers."""

from __future__ import annotations

from kit.clock import SimClock
from kit.rng import SeededRNG

from ..common import WorldService
from .app import create_app
from .core import Directory


class OrgService(WorldService):
    """The org service one episode holds."""

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.directory = Directory(rng.child("org"))
        super().__init__("org", create_app(self.directory), self.directory.store, clock)


__all__ = ["Directory", "OrgService"]
