"""Incident tickets: classification, comments, escalation on an SLA timer, reports.

A ticket's SLA runs from its first alert by its severity. If it is still open
and not escalated when the timer falls due (``tickets.sla_due`` on the
simulated clock), it is marked breached and the SLA monitor says so in a
comment. Reports are versioned: a second submission is a new version.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field

from kit.clock import ScheduledEvent
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.types import Strict

from ...vocab import Classification, Severity
from ..common import UNKNOWN_RESOURCE, Result, Row, SqlCore, dumps, iso, parse_time

EVENT_SLA_DUE = "tickets.sla_due"
TICKET_NOT_FOUND = "ticket_not_found"
CLASSIFICATION_REQUIRED = "classification_required"
ALREADY_ESCALATED = "already_escalated"
NOTHING_TO_UPDATE = "nothing_to_update"
UNKNOWN_EVENT = "unknown_event"
ANALYST = "analyst"
SLA_MONITOR = "sla-monitor"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE ticket (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, severity TEXT NOT NULL,
    status TEXT NOT NULL, classification TEXT, sla_due_at TEXT NOT NULL,
    sla_breached INTEGER NOT NULL, escalated_at TEXT, escalation_severity TEXT,
    escalation_summary TEXT, recommended_actions TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE comment (
    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, at TEXT NOT NULL, author TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE report (
    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, version INTEGER NOT NULL,
    submitted_at TEXT NOT NULL, fields TEXT NOT NULL
);
"""

ADMIN_RESOURCES = ("state", "meta", "tickets", "comments", "reports")


class LoadRequest(Strict):
    """Tickets and their comments, the start time and the SLA table."""

    started_at: str
    sla_minutes: dict[str, int]
    tickets: list[dict[str, Any]]
    comments: list[dict[str, Any]] = Field(default_factory=list)


class EventRequest(Strict):
    """A due SLA timer."""

    name: str
    payload: dict[str, Any]


class TicketUpdate(Strict):
    """What ``update_ticket`` may change; at least one field."""

    classification: Classification | None = None
    status: Literal["open", "closed"] | None = None
    comment: str | None = Field(default=None, min_length=1, max_length=4_000)


class Escalation(Strict):
    """Paging on-call for a ticket."""

    severity: Severity
    summary: str = Field(min_length=1, max_length=4_000)
    recommended_actions: list[dict[str, str]] = Field(default_factory=list)


class ReportSubmission(Strict):
    """A structured incident report; the tool validated its fields."""

    fields: dict[str, Any]


class Tickets(SqlCore):
    """The tickets service's rules."""

    service = "tickets"

    def __init__(self, rng: SeededRNG) -> None:
        super().__init__(rng, SCHEMA)

    def load(self, body: LoadRequest, now: Any) -> Result:
        """Store tickets; start each open one's SLA timer, or mark it breached already."""
        self._refuse_reload()
        scheduled: list[ScheduledEvent] = []
        with self._transaction():
            self._insert("meta", {"key": "started_at", "value": body.started_at})
            self._insert("meta", {"key": "sla_minutes", "value": dumps(body.sla_minutes)})
            for ticket in body.tickets:
                settled = ticket["escalated_at"] is not None or ticket["status"] == "closed"
                due = parse_time(ticket["sla_due_at"])
                self._insert(
                    "ticket",
                    {
                        **ticket,
                        "sla_breached": int(not settled and due <= now),
                        "escalation_severity": None,
                        "escalation_summary": None,
                        "recommended_actions": "[]",
                        "closed_at": None,
                    },
                )
                if not settled and due > now:
                    scheduled.append(
                        ScheduledEvent(
                            at=due, name=EVENT_SLA_DUE, payload={"ticket_id": ticket["id"]}
                        )
                    )
            for comment in body.comments:
                self._add_comment(
                    comment["ticket_id"], comment["at"], comment["author"], comment["body"]
                )
        return Result(body={"tickets": len(body.tickets)}, scheduled=scheduled, created=True)

    def apply_event(self, body: EventRequest, now: Any) -> Result:
        """An SLA timer fell due: breach the ticket if nobody escalated or closed it."""
        if body.name != EVENT_SLA_DUE:
            msg = f"tickets has no event {body.name!r}"
            raise ToolRejected(UNKNOWN_EVENT, msg)
        ticket = self._ticket_row(str(body.payload["ticket_id"]))
        if ticket["escalated_at"] is not None or ticket["status"] == "closed":
            return Result(body={"breached": False})
        with self._transaction():
            diff = self._set("ticket", ticket, sla_breached=1)
            comment = self._add_comment(
                ticket["id"],
                iso(now),
                SLA_MONITOR,
                f"SLA breached: not escalated or closed by {ticket['sla_due_at']}.",
            )
            diff.append(self._created("comment", comment))
        return Result(body={"breached": True}, state_diff=diff)

    # ----------------------------------------------------------------- writes
    def update(self, ticket_id: str, body: TicketUpdate, now: Any) -> Result:
        """Classify, close or reopen a ticket, or comment on it."""
        if body.classification is None and body.status is None and body.comment is None:
            msg = "give at least one of classification, status, comment"
            raise ToolRejected(NOTHING_TO_UPDATE, msg)
        ticket = self._ticket_row(ticket_id)
        classification = (
            body.classification.value if body.classification else ticket["classification"]
        )
        if body.status == "closed" and classification is None:
            msg = "classify the ticket before closing it"
            raise ToolRejected(CLASSIFICATION_REQUIRED, msg)
        with self._transaction():
            changes: dict[str, Any] = {"classification": classification}
            if body.status is not None:
                changes["status"] = body.status
                changes["closed_at"] = iso(now) if body.status == "closed" else None
            diff = self._set("ticket", ticket, **changes)
            if body.comment is not None:
                comment = self._add_comment(ticket_id, iso(now), ANALYST, body.comment)
                diff.append(self._created("comment", comment))
        return Result(body={"ticket": self.ticket(ticket_id)}, state_diff=diff)

    def escalate(self, ticket_id: str, body: Escalation, now: Any) -> Result:
        """Page on-call for a ticket. Once per ticket."""
        ticket = self._ticket_row(ticket_id)
        if ticket["escalated_at"] is not None:
            msg = f"{ticket_id} was already escalated at {ticket['escalated_at']}"
            raise ToolRejected(ALREADY_ESCALATED, msg)
        with self._transaction():
            diff = self._set(
                "ticket",
                ticket,
                escalated_at=iso(now),
                escalation_severity=body.severity.value,
                escalation_summary=body.summary,
                recommended_actions=dumps(body.recommended_actions),
            )
        return Result(body={"ticket": self.ticket(ticket_id)}, state_diff=diff)

    def submit_report(self, ticket_id: str, body: ReportSubmission, now: Any) -> Result:
        """Store a new version of a ticket's report."""
        self._ticket_row(ticket_id)
        version = (
            int(self._scalar("SELECT COUNT(*) FROM report WHERE ticket_id = ?", (ticket_id,))) + 1
        )
        row = {
            "id": self._next_id("rpt", "report"),
            "ticket_id": ticket_id,
            "version": version,
            "submitted_at": iso(now),
            "fields": dumps(body.fields),
        }
        with self._transaction():
            self._insert("report", row)
        return Result(
            body={"report": _report(dict(row))},
            state_diff=[self._created("report", row)],
            created=True,
        )

    def _add_comment(self, ticket_id: str, at: str, author: str, text: str) -> Row:
        row = {
            "id": self._next_id("cmt", "comment"),
            "ticket_id": ticket_id,
            "at": at,
            "author": author,
            "body": text,
        }
        self._insert("comment", row)
        return row

    # ------------------------------------------------------------------ reads
    def _ticket_row(self, ticket_id: str) -> Row:
        row = self._one("SELECT * FROM ticket WHERE id = ?", (ticket_id,))
        if row is None:
            msg = f"no ticket {ticket_id!r}"
            raise ToolRejected(TICKET_NOT_FOUND, msg)
        return row

    def ticket(self, ticket_id: str) -> Row:
        """A ticket, its comments, and whether a report was filed."""
        row = _ticket(self._ticket_row(ticket_id))
        row["comments"] = self.store.query_all(
            "SELECT at, author, body FROM comment WHERE ticket_id = ? ORDER BY at, rowid",
            (ticket_id,),
        )
        row["reports_filed"] = int(
            self._scalar("SELECT COUNT(*) FROM report WHERE ticket_id = ?", (ticket_id,))
        )
        return row

    def admin(self, resource: str) -> Any:
        """The true state, for graders and tests only. Reads, never writes."""
        if resource not in ADMIN_RESOURCES:
            msg = f"unknown resource {resource!r}; tickets has {list(ADMIN_RESOURCES)}"
            raise ToolRejected(UNKNOWN_RESOURCE, msg)
        if resource == "state":
            return {name: self.admin(name) for name in ADMIN_RESOURCES[1:]}
        if resource == "meta":
            rows = {row["key"]: row["value"] for row in self.store.query_all("SELECT * FROM meta")}
            return {
                "started_at": rows["started_at"],
                "sla_minutes": json.loads(rows["sla_minutes"]),
            }
        if resource == "tickets":
            return [
                _ticket(row) for row in self.store.query_all("SELECT * FROM ticket ORDER BY id")
            ]
        if resource == "comments":
            return self.store.query_all("SELECT * FROM comment ORDER BY at, rowid")
        return [_report(row) for row in self.store.query_all("SELECT * FROM report ORDER BY rowid")]


def _ticket(row: Row) -> Row:
    row["recommended_actions"] = json.loads(row["recommended_actions"])
    row["sla_breached"] = bool(row["sla_breached"])
    return row


def _report(row: Row) -> Row:
    row["fields"] = json.loads(row["fields"])
    return row


__all__ = [
    "ADMIN_RESOURCES",
    "EVENT_SLA_DUE",
    "Escalation",
    "EventRequest",
    "LoadRequest",
    "ReportSubmission",
    "TicketUpdate",
    "Tickets",
]
