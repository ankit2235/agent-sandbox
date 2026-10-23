"""The payments episode: rails, commerce and disputes behind one set of tools.

Reset, snapshot and restore come from :class:`kit.episode.BaseEpisode`, over
all three services. Scheduled events are routed to the service that scheduled
them by name (``rails.*``, ``commerce.*``, ``disputes.*``), and their effects on
the other services are carried by :class:`~.links.Links`.

A task's ``history`` is replayed in ``_setup``, before the kit takes the
baseline snapshot: through the tools' own handlers, with faults off and nothing
traced, so the agent starts in a world where those purchases really happened and
graders can tell them apart from the agent's (they are not in the trace).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from kit.clock import ScheduledEvent, SimClock
from kit.episode import BaseEpisode, ToolContext, ToolHandler, ToolResult, ToolRouter
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.trace import DiffEntry
from kit.types import TaskSpec

from .links import Links
from .services.commerce import CommerceService
from .services.common import iso
from .services.disputes import DisputesService
from .services.rails import RailsService
from .state import PastPayment, PastPurchase, PaymentsState, shifted
from .tools import args as a
from .tools.handlers import add_to_cart, apply_offer, build_router, checkout, parse, pay


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
        raw = shifted(task.initial_state, state.history_seconds)
        faults = raw["rails"].get("faults")
        if state.history:
            raw["rails"].pop("faults", None)
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
        if state.history:
            self._replay(task, state)
            self.rails.call("POST", "/internal/faults", faults or {})

    def _replay(self, task: TaskSpec, state: PaymentsState) -> None:
        """Run the task's history, oldest first, then move the clock on to the present."""
        present = self.clock.now() + dt.timedelta(seconds=state.history_seconds)
        numbered = sorted(enumerate(state.history, start=1), key=lambda pair: -pair[1].seconds_ago)
        for number, entry in numbered:
            self._quietly_until(present - dt.timedelta(seconds=entry.seconds_ago))
            key = f"history-{number}"
            if isinstance(entry, PastPayment):
                args = {
                    "payee": entry.payee,
                    "amount_paise": entry.amount_paise,
                    "idempotency_key": key,
                    "note": entry.note,
                }
            else:
                assert isinstance(entry, PastPurchase)
                merchant = {"merchant_id": entry.merchant_id}
                for item in entry.items:
                    self._history_tool(task, number, add_to_cart, {**merchant, **item.model_dump()})
                if entry.offer_code is not None:
                    self._history_tool(
                        task, number, apply_offer, {**merchant, "code": entry.offer_code}
                    )
                due = self._history_tool(task, number, checkout, merchant)["payment_request"]
                if due is None:
                    continue
                args = {
                    "payee": due["payee"],
                    "amount_paise": due["amount_paise"],
                    "idempotency_key": key,
                    "order_id": due["order_id"],
                }
            paid = self._history_tool(task, number, pay, args)
            if paid["outcome"] != "paid":
                msg = f"history entry {number}: the payment was {paid['outcome']}: {paid}"
                raise ValueError(msg)
        self._quietly_until(present)

    def _history_tool(
        self, task: TaskSpec, number: int, handler: ToolHandler, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Call one tool handler for a history entry, outside the trace."""
        context = ToolContext(
            task=task,
            clock=self.clock,
            rng=self._rng.child(f"history/{number}/{handler.__name__}"),
            services=self.services,
            step_index=0,
        )
        try:
            return handler(args, context).data
        except ToolRejected as refused:
            msg = (
                f"history entry {number}: {handler.__name__} refused: "
                f"{refused.code}: {refused.message}"
            )
            raise ValueError(msg) from None

    def _quietly_until(self, moment: dt.datetime) -> None:
        """Move the clock to ``moment``, applying due events without tracing them."""
        while (due := self.clock.next_event_at()) is not None and due <= moment:
            self.clock.advance(max(due - self.clock.now(), dt.timedelta(0)))
            for event in self.clock.due():
                self._on_event(event)
        self.clock.advance(max(moment - self.clock.now(), dt.timedelta(0)))

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
