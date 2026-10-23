"""The payments episode: rails, commerce and disputes behind one set of tools.

Reset, snapshot and restore come from :class:`kit.episode.BaseEpisode`, over
all three services. Scheduled events are routed to the service that scheduled
them by name (``rails.*``, ``commerce.*``, ``disputes.*``), and their effects on
the other services are carried by :class:`~.links.Links`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from kit.clock import ScheduledEvent, SimClock
from kit.episode import BaseEpisode, ToolContext, ToolResult, ToolRouter
from kit.rng import SeededRNG
from kit.trace import DiffEntry
from kit.types import TaskSpec

from .links import Links
from .services.commerce import CommerceService
from .services.common import iso
from .services.disputes import DisputesService
from .services.rails import RailsService
from .state import PaymentsState
from .tools import args as a
from .tools.handlers import build_router, parse


class PaymentsEpisode(BaseEpisode):
    """One payments task: three services, the tools, and the events between them."""

    world = "payments"

    def __init__(self, task: TaskSpec, clock: SimClock, rng: SeededRNG) -> None:
        self.rails = RailsService(clock, rng)
        self.commerce = CommerceService(clock, rng)
        self.disputes = DisputesService(clock, rng)
        super().__init__(
            [self.rails, self.commerce, self.disputes], build_router(self._wait), clock, rng
        )
        self._links: Links | None = None

    @property
    def router(self) -> ToolRouter:
        """The tools this episode serves, for the MCP bridge."""
        return self._router

    @property
    def links(self) -> Links:
        """The cross-service plumbing, once a task is loaded."""
        if self._links is None:
            msg = "the episode has not been reset with a task yet"
            raise RuntimeError(msg)
        return self._links

    def _setup(self, task: TaskSpec) -> None:
        """Validate the whole ``initial_state``, then give each service its part."""
        state = PaymentsState.model_validate(task.initial_state)
        raw = task.initial_state
        self.rails.load(raw["rails"])
        self.commerce.load({"user": state.user, "config": raw.get("commerce", {})})
        self.disputes.load(
            {
                "user": state.user,
                "merchants": raw.get("commerce", {}).get("merchants", []),
                "config": raw.get("disputes", {}),
            }
        )
        self._links = Links(self.rails, self.commerce, self.disputes, state.user)

    def _on_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        return self.links.on_event(event)

    def _wait(self, raw: dict[str, Any], context: ToolContext) -> ToolResult:
        """Let time pass and deliver every event that falls due, inside this step.

        The kit drains events before a tool runs; a wait that only moved the clock
        would show nothing until the next call. Here the clock stops at each event's
        own time, so a delivery due on day 3 of a week-long wait happens on day 3,
        and the events are recorded in the trace ahead of this ``wait`` call.
        The agent is told what a person would notice: new messages, not events.
        """
        args = parse(a.Wait, raw)
        clock = context.clock
        since = clock.now()
        target = since + dt.timedelta(hours=args.hours)
        before = self.trace.next_index
        while (due := clock.next_event_at()) is not None and due <= target:
            clock.advance(max(due - clock.now(), dt.timedelta(0)))
            self._drain_events()
        clock.advance(target - clock.now())
        fired = sum(1 for step in self.trace.steps[before:] if step.kind == "event")
        arrived = [
            {"id": row["id"], "subject": row["subject"]}
            for row in self.disputes.query("messages")
            if row["direction"] == "in" and row["created_at"] > iso(since)
        ]
        now = context.clock.now()
        text = f"Waited {args.hours:g} hours; it is now {now.isoformat()}."
        if arrived:
            text += f" {len(arrived)} new message(s) in your inbox:\n" + "\n".join(
                f"[{item['id']}] {item['subject']}" for item in arrived
            )
        else:
            text += " No new messages."
        return ToolResult(
            text=text,
            data={"now": now.isoformat(), "events_fired": fired, "new_messages": arrived},
        )


def make_episode(task: TaskSpec, clock: SimClock, rng: SeededRNG) -> PaymentsEpisode:
    """Episode factory for the world registry."""
    return PaymentsEpisode(task, clock, rng)


__all__ = ["PaymentsEpisode", "make_episode"]
