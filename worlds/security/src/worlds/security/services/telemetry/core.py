"""The log store: auth, endpoint process, network flow, DNS and cloud audit events.

Queries never return an event later than the simulated now. Story events still
in the future are delivered on the clock as ``telemetry.beat``; one with
``unless`` is dropped when the episode reports that containment is in force.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.types import Strict

from ...scenarios.noise import EVENT_FIELDS
from ..common import UNKNOWN_RESOURCE, Result, Row, SqlCore, iso, parse_time

EVENT_BEAT = "telemetry.beat"
UNKNOWN_EVENT = "unknown_event"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE event (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,
    host TEXT, user TEXT, src_ip TEXT, dst_ip TEXT, dst_port INTEGER, domain TEXT,
    process TEXT, parent_process TEXT, action TEXT, outcome TEXT, bytes_out INTEGER,
    geo TEXT, app TEXT, sha256 TEXT, message TEXT NOT NULL
);
CREATE INDEX event_ts ON event (ts, id);
"""

COLUMNS = ("id", "ts", "source", *EVENT_FIELDS)
SEARCHED = ("message", "process", "parent_process", "domain", "action", "app", "geo")
"""Columns ``contains`` looks in."""

ADMIN_RESOURCES = ("state", "events")


class LoadRequest(Strict):
    """Past events, and future story events to put on the clock."""

    events: list[dict[str, Any]]
    future: list[dict[str, Any]] = Field(default_factory=list)


class EventRequest(Strict):
    """A due event, with ``suppressed`` handed in by the episode."""

    name: str
    payload: dict[str, Any]


class Filters(Strict):
    """What ``search_logs`` and ``timeline`` narrow by. All given filters must match."""

    source: str | None = None
    host: str | None = None
    user: str | None = None
    ip: str | None = None
    domain: str | None = None
    process: str | None = None
    contains: str | None = None
    entities: list[str] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)
    since: str | None = None
    until: str | None = None
    limit: int = 50


class Telemetry(SqlCore):
    """The telemetry service's rules."""

    service = "telemetry"

    def __init__(self, rng: SeededRNG) -> None:
        super().__init__(rng, SCHEMA)

    def load(self, body: LoadRequest) -> Result:
        """Store past events; return the future ones as events for the clock."""
        self._refuse_reload()
        with self._transaction():
            self._insert("meta", {"key": "loaded", "value": "1"})
            for row in body.events:
                self._insert("event", {column: row[column] for column in COLUMNS})
        scheduled = [
            ScheduledEvent(
                at=parse_time(item["at"]),
                name=EVENT_BEAT,
                payload={"beat": item["beat"], "event": item["event"], "unless": item["unless"]},
            )
            for item in body.future
        ]
        return Result(body={"events": len(body.events)}, scheduled=scheduled, created=True)

    def apply_event(self, body: EventRequest) -> Result:
        """Record a story event that has fallen due, unless the episode says it was stopped."""
        if body.name != EVENT_BEAT:
            msg = f"telemetry has no event {body.name!r}"
            raise ToolRejected(UNKNOWN_EVENT, msg)
        if body.payload.get("suppressed"):
            return Result(body={"recorded": False})
        row = {column: body.payload["event"][column] for column in COLUMNS}
        with self._transaction():
            self._insert("event", row)
        return Result(
            body={"recorded": True}, state_diff=[self._created("event", row)], created=True
        )

    def search(self, filters: Filters, now: Any) -> Row:
        """Events matching every filter, oldest first, up to ``limit``, with the total."""
        clauses = ["ts <= ?"]
        params: list[Any] = [iso(now)]
        for column in ("source", "host", "user", "domain"):
            value = getattr(filters, column)
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if filters.ip is not None:
            clauses.append("(src_ip = ? OR dst_ip = ?)")
            params += [filters.ip, filters.ip]
        if filters.process is not None:
            clauses.append("(process = ? OR parent_process = ?)")
            params += [filters.process, filters.process]
        if filters.contains:
            pattern = "%" + _escape(filters.contains.lower()) + "%"
            clauses.append(
                "(" + " OR ".join(f"lower({c}) LIKE ? ESCAPE '\\'" for c in SEARCHED) + ")"
            )
            params += [pattern] * len(SEARCHED)
        if filters.entities:
            marks = ", ".join("?" for _ in filters.entities)
            clauses.append(
                "("
                + " OR ".join(
                    f"{c} IN ({marks})" for c in ("host", "user", "src_ip", "dst_ip", "domain")
                )
                + ")"
            )
            params += list(filters.entities) * 5
        if filters.ids:
            clauses.append(f"id IN ({', '.join('?' for _ in filters.ids)})")
            params += list(filters.ids)
        if filters.since is not None:
            clauses.append("ts >= ?")
            params.append(filters.since)
        if filters.until is not None:
            clauses.append("ts <= ?")
            params.append(filters.until)
        where = " AND ".join(clauses)
        total = int(self._scalar(f"SELECT COUNT(*) FROM event WHERE {where}", tuple(params)))
        rows = self.store.query_all(
            f"SELECT * FROM event WHERE {where} ORDER BY ts, id LIMIT ?",
            (*params, filters.limit),
        )
        return {"total": total, "events": rows}

    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; telemetry has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(UNKNOWN_RESOURCE, msg)
        events = self.store.query_all("SELECT * FROM event ORDER BY ts, id")
        return {"events": events} if resource == "state" else events


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["ADMIN_RESOURCES", "EVENT_BEAT", "EventRequest", "Filters", "LoadRequest", "Telemetry"]
