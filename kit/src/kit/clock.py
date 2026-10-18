"""Simulated time.

No code in the sandbox may read a wall clock. Time only moves because an
episode advanced it, which is what makes a deadline, a timeout or a month of
subscriptions reproducible.
"""

from __future__ import annotations

import datetime as dt
import heapq
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EPOCH = dt.datetime(2026, 1, 1, 9, 0, 0, tzinfo=dt.UTC)
"""Default episode start: a Thursday morning, so weekday rules are exercised."""


class ScheduledEvent(BaseModel):
    """Something the world makes happen at a simulated time, not the agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: dt.datetime
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SimClock:
    """A deterministic clock with a queue of scheduled events.

    The episode owns the clock and is the only thing that advances it. Services
    receive the current simulated time on every request (see
    :data:`kit.service.SIM_TIME_HEADER`) and never read one themselves.
    """

    def __init__(self, start: dt.datetime = EPOCH) -> None:
        if start.tzinfo is None:
            msg = "SimClock start must be timezone-aware"
            raise ValueError(msg)
        self._start = start.astimezone(dt.UTC)
        self._now = self._start
        self._queue: list[tuple[dt.datetime, int, ScheduledEvent]] = []
        self._sequence = 0

    @property
    def start(self) -> dt.datetime:
        """The time the episode began."""
        return self._start

    def now(self) -> dt.datetime:
        """Current simulated time, always UTC."""
        return self._now

    @property
    def elapsed(self) -> dt.timedelta:
        """How much simulated time has passed since the episode started."""
        return self._now - self._start

    def advance(self, seconds: float | dt.timedelta = 0) -> dt.datetime:
        """Move time forward. Never backwards."""
        delta = seconds if isinstance(seconds, dt.timedelta) else dt.timedelta(seconds=seconds)
        if delta < dt.timedelta(0):
            msg = "simulated time cannot move backwards"
            raise ValueError(msg)
        self._now += delta
        return self._now

    def schedule(self, event: ScheduledEvent) -> ScheduledEvent:
        """Queue an event. Events at the same instant fire in the order scheduled."""
        at = event.at.astimezone(dt.UTC)
        if at < self._start:
            msg = f"event {event.name!r} is scheduled before the episode started"
            raise ValueError(msg)
        heapq.heappush(self._queue, (at, self._sequence, event))
        self._sequence += 1
        return event

    def schedule_in(
        self, seconds: float | dt.timedelta, name: str, payload: dict[str, Any] | None = None
    ) -> ScheduledEvent:
        """Queue an event relative to now."""
        delta = seconds if isinstance(seconds, dt.timedelta) else dt.timedelta(seconds=seconds)
        return self.schedule(ScheduledEvent(at=self._now + delta, name=name, payload=payload or {}))

    def due(self) -> list[ScheduledEvent]:
        """Pop every event whose time has arrived, oldest first."""
        fired: list[ScheduledEvent] = []
        while self._queue and self._queue[0][0] <= self._now:
            fired.append(heapq.heappop(self._queue)[2])
        return fired

    @property
    def pending(self) -> list[ScheduledEvent]:
        """Events still in the future, in firing order. Read-only."""
        return [event for _, _, event in sorted(self._queue, key=lambda item: item[:2])]

    def next_event_at(self) -> dt.datetime | None:
        """When the next scheduled event fires, if any."""
        return self._queue[0][0] if self._queue else None

    def __repr__(self) -> str:
        return f"SimClock(now={self._now.isoformat()}, pending={len(self._queue)})"
