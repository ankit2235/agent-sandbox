"""The SIEM alert queue. Alerts are raised by the story; most are noise or benign.

An alert's triage state lives on its ticket, in the tickets service.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.types import Strict

from ..common import UNKNOWN_RESOURCE, Result, Row, SqlCore, dumps, iso, parse_time

EVENT_FIRE = "alerts.fire"
ALERT_NOT_FOUND = "alert_not_found"
UNKNOWN_EVENT = "unknown_event"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE alert (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, rule TEXT NOT NULL, severity TEXT NOT NULL,
    description TEXT NOT NULL, host TEXT, user TEXT, ip TEXT, domain TEXT,
    event_ids TEXT NOT NULL, ticket_id TEXT NOT NULL
);
"""

ADMIN_RESOURCES = ("state", "alerts")


class LoadRequest(Strict):
    """Alerts already raised, and future ones to put on the clock."""

    alerts: list[dict[str, Any]]
    future: list[dict[str, Any]] = Field(default_factory=list)


class EventRequest(Strict):
    """A due event, with ``suppressed`` handed in by the episode."""

    name: str
    payload: dict[str, Any]


class Alerts(SqlCore):
    """The alerts service's rules."""

    service = "alerts"

    def __init__(self, rng: SeededRNG) -> None:
        super().__init__(rng, SCHEMA)

    def load(self, body: LoadRequest) -> Result:
        """Store raised alerts; return future ones as events for the clock."""
        self._refuse_reload()
        with self._transaction():
            self._insert("meta", {"key": "loaded", "value": "1"})
            for row in body.alerts:
                self._insert("alert", self._stored(row))
        scheduled = [
            ScheduledEvent(
                at=parse_time(item["at"]),
                name=EVENT_FIRE,
                payload={"alert": item["alert"], "unless": item["unless"]},
            )
            for item in body.future
        ]
        return Result(body={"alerts": len(body.alerts)}, scheduled=scheduled, created=True)

    def apply_event(self, body: EventRequest) -> Result:
        """Raise a future alert, unless the episode says what it fires on was stopped."""
        if body.name != EVENT_FIRE:
            msg = f"alerts has no event {body.name!r}"
            raise ToolRejected(UNKNOWN_EVENT, msg)
        if body.payload.get("suppressed"):
            return Result(body={"raised": False})
        row = self._stored(body.payload["alert"])
        with self._transaction():
            self._insert("alert", row)
        return Result(body={"raised": True}, state_diff=[self._created("alert", row)], created=True)

    def alerts(
        self, now: Any, *, severity: str | None, ticket_id: str | None, limit: int
    ) -> list[Row]:
        """Raised alerts, newest first."""
        clauses, params = ["created_at <= ?"], [iso(now)]
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if ticket_id is not None:
            clauses.append("ticket_id = ?")
            params.append(ticket_id)
        where = " AND ".join(clauses)
        rows = self.store.query_all(
            f"SELECT * FROM alert WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [_decoded(row) for row in rows]

    def alert(self, alert_id: str, now: Any) -> Row:
        """One raised alert."""
        row = self._one(
            "SELECT * FROM alert WHERE id = ? AND created_at <= ?", (alert_id, iso(now))
        )
        if row is None:
            msg = f"no alert {alert_id!r}"
            raise ToolRejected(ALERT_NOT_FOUND, msg)
        return _decoded(row)

    @staticmethod
    def _stored(row: dict[str, Any]) -> Row:
        return {**row, "event_ids": dumps(row["event_ids"])}

    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; alerts has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(UNKNOWN_RESOURCE, msg)
        rows = [
            _decoded(r) for r in self.store.query_all("SELECT * FROM alert ORDER BY created_at, id")
        ]
        return {"alerts": rows} if resource == "state" else rows


def _decoded(row: Row) -> Row:
    row["event_ids"] = json.loads(row["event_ids"])
    return row


__all__ = [
    "ADMIN_RESOURCES",
    "ALERT_NOT_FOUND",
    "EVENT_FIRE",
    "Alerts",
    "EventRequest",
    "LoadRequest",
]
