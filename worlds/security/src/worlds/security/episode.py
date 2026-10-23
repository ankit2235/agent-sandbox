"""The security episode: five services behind one set of tools.

Reset, snapshot and restore come from :class:`kit.episode.BaseEpisode`. In
``_setup`` the task's story is expanded into each service's rows (the ground
truth stays behind). Scheduled events go to the service whose name prefixes
them; a future beat or alert with ``unless`` is handed the fact of whether that
containment is in force, read from the response service (services never call
each other).

Each tool call takes one simulated minute (decision 91): an analyst's action is
not instant, so a 15-minute SLA can be missed.
"""

from __future__ import annotations

from typing import Any

from kit.clock import ScheduledEvent, SimClock
from kit.episode import BaseEpisode, ToolRouter
from kit.rng import SeededRNG
from kit.trace import DiffEntry
from kit.types import TaskSpec

from .scenarios import expand
from .services.alerts import AlertsService
from .services.common import WorldService, iso
from .services.org import OrgService
from .services.response import ResponseService
from .services.telemetry import TelemetryService
from .services.tickets import TicketsService
from .state import SecurityState, org_payload
from .tools.handlers import build_router

CONDITIONS = ("isolated", "disabled", "blocked", "reset")


class SecurityEpisode(BaseEpisode):
    """One security task: org, telemetry, alerts, response and tickets."""

    world = "security"
    step_seconds = 60.0

    def __init__(self, task: TaskSpec, clock: SimClock, rng: SeededRNG) -> None:
        self.org = OrgService(clock, rng)
        self.telemetry = TelemetryService(clock, rng)
        self.alerts = AlertsService(clock, rng)
        self.response = ResponseService(clock, rng)
        self.tickets = TicketsService(clock, rng)
        self._world_services: list[WorldService] = [
            self.org,
            self.telemetry,
            self.alerts,
            self.response,
            self.tickets,
        ]
        super().__init__(self._world_services, build_router(), clock, rng)

    @property
    def router(self) -> ToolRouter:
        """The tools this episode serves, for the MCP bridge."""
        return self._router

    def _setup(self, task: TaskSpec) -> None:
        """Expand the task's story and give each service its rows."""
        state = SecurityState.model_validate(task.initial_state)
        start = self.clock.now()
        world = expand(state.story(), start, self._rng.child("scenario"), state.sla_minutes.table())
        self.org.load(org_payload(world.org, start))
        self.telemetry.load({"events": world.events, "future": world.future_events})
        self.alerts.load({"alerts": world.alerts, "future": world.future_alerts})
        self.response.load(
            {
                "evidence": world.evidence,
                "approval_required_at": state.analyst.approval_required_at,
                "prior_actions": world.prior_actions,
            }
        )
        self.tickets.load(
            {
                "started_at": iso(start),
                "sla_minutes": {k.value: v for k, v in state.sla_minutes.table().items()},
                "tickets": world.tickets,
                "comments": world.comments,
            }
        )

    def _on_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        for service in self._world_services:
            if service.handles(event):
                extra: dict[str, Any] = {}
                unless = event.payload.get("unless")
                if unless:
                    extra["suppressed"] = self._in_force(unless)
                return service.deliver(event, extra)
        msg = f"no service handles event {event.name!r}"
        raise RuntimeError(msg)

    def _in_force(self, condition: dict[str, str]) -> bool:
        """Whether the containment a condition names is in force right now."""
        current = self.response.call("GET", "/internal/containment")
        return any(condition.get(name) in current[name] for name in CONDITIONS)


def make_episode(task: TaskSpec, clock: SimClock, rng: SeededRNG) -> SecurityEpisode:
    """Episode factory for the world registry."""
    return SecurityEpisode(task, clock, rng)


__all__ = ["SecurityEpisode", "make_episode"]
