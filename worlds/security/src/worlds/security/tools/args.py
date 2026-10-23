"""Typed arguments for every security tool.

Each model's JSON Schema is the tool's MCP ``inputSchema``; the same model
validates the call. Every id has a strict pattern (payments decision 81), so
nothing the agent types can walk a service path out to ``/admin`` or
``/internal``.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictInt, model_validator

from kit.types import Strict

from ..vocab import (
    ACTION_PATTERN,
    ALERT_PATTERN,
    DOMAIN_PATTERN,
    ENTITY_PATTERN,
    HOST_PATTERN,
    IP_PATTERN,
    PROCESS_PATTERN,
    SHA256_PATTERN,
    TICKET_PATTERN,
    USER_PATTERN,
    Classification,
    EntityKind,
    IncidentType,
    IndicatorKind,
    Severity,
    Source,
    Verdict,
)

HostId = Annotated[str, Field(pattern=HOST_PATTERN, description="A host id, e.g. ws-003.")]
UserId = Annotated[str, Field(pattern=USER_PATTERN, description="An account id, e.g. priya.n.")]
AlertId = Annotated[str, Field(pattern=ALERT_PATTERN, description="An alert id, e.g. ALR-1041.")]
TicketId = Annotated[str, Field(pattern=TICKET_PATTERN, description="A ticket id, e.g. INC-3307.")]
ActionId = Annotated[str, Field(pattern=ACTION_PATTERN, description="A response action's id.")]
Ip = Annotated[str, Field(pattern=IP_PATTERN)]
Domain = Annotated[str, Field(pattern=DOMAIN_PATTERN, max_length=253)]
Reason = Annotated[str, Field(min_length=1, max_length=500, description="Why, for the action log.")]
Limit = Annotated[StrictInt, Field(ge=1, le=200)]


class ToolArgs(Strict):
    """Base for tool arguments: unknown fields are refused."""


class ListAlerts(ToolArgs):
    """Arguments for ``list_alerts``."""

    severity: Severity | None = Field(default=None, description="Only alerts of this severity.")
    status: Literal["open", "closed"] | None = Field(
        default=None, description="Only alerts whose ticket is open, or closed."
    )
    limit: Limit = Field(default=50, description="How many, newest first.")


class GetAlert(ToolArgs):
    """Arguments for ``get_alert``."""

    alert_id: AlertId


class TimeRange(ToolArgs):
    """A time window and a row limit, shared by the log tools."""

    since: AwareDatetime | None = Field(default=None, description="Only events at or after this.")
    until: AwareDatetime | None = Field(default=None, description="Only events at or before this.")
    limit: Limit = Field(default=50, description="How many events, oldest first.")

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.since is not None and self.until is not None and self.since > self.until:
            msg = "since must not be after until"
            raise ValueError(msg)
        return self


class SearchLogs(TimeRange):
    """Arguments for ``search_logs``. Every filter given must match."""

    source: Source | None = Field(default=None, description="One log source.")
    host: HostId | None = None
    user: UserId | None = None
    ip: Ip | None = Field(default=None, description="Matches the source or destination address.")
    domain: Domain | None = None
    process: Annotated[str, Field(pattern=PROCESS_PATTERN)] | None = Field(
        default=None, description="Matches the process or its parent."
    )
    contains: Annotated[str, Field(min_length=1, max_length=64)] | None = Field(
        default=None, description="Text in the message, process, domain, action, app or location."
    )


class Timeline(TimeRange):
    """Arguments for ``timeline``."""

    entity: Annotated[str, Field(pattern=ENTITY_PATTERN, max_length=253)] = Field(
        description="A host id, account id, IP address or domain."
    )


class GetHost(ToolArgs):
    """Arguments for ``get_host``."""

    host_id: HostId


class GetUser(ToolArgs):
    """Arguments for ``get_user``."""

    user_id: UserId


class HostAction(ToolArgs):
    """Arguments for an action on a host."""

    host_id: HostId
    reason: Reason


class AccountAction(ToolArgs):
    """Arguments for an action on an account."""

    user_id: UserId
    reason: Reason


class BlockIndicator(ToolArgs):
    """Arguments for ``block_indicator``."""

    kind: IndicatorKind
    value: Annotated[str, Field(min_length=1, max_length=253)]
    reason: Reason

    @model_validator(mode="after")
    def _shape(self) -> BlockIndicator:
        pattern = {
            IndicatorKind.IP: IP_PATTERN,
            IndicatorKind.DOMAIN: DOMAIN_PATTERN,
            IndicatorKind.SHA256: SHA256_PATTERN,
        }[self.kind]
        if not re.match(pattern, self.value):
            msg = f"{self.value!r} is not a valid {self.kind.value}"
            raise ValueError(msg)
        return self


class Restore(ToolArgs):
    """Arguments for ``restore``."""

    action_id: ActionId
    reason: Reason


class UpdateTicket(ToolArgs):
    """Arguments for ``update_ticket``: at least one change."""

    ticket_id: TicketId
    classification: Classification | None = None
    status: Literal["open", "closed"] | None = Field(
        default=None, description="Closing needs a classification."
    )
    comment: Annotated[str, Field(min_length=1, max_length=4_000)] | None = None

    @model_validator(mode="after")
    def _something(self) -> UpdateTicket:
        if self.classification is None and self.status is None and self.comment is None:
            msg = "give at least one of classification, status, comment"
            raise ValueError(msg)
        return self


class RecommendedAction(ToolArgs):
    """A containment action the agent recommends to on-call."""

    action: Literal[
        "isolate_host", "disable_account", "reset_credentials", "block_indicator", "reimage_host"
    ]
    target: Annotated[str, Field(pattern=ENTITY_PATTERN, max_length=253)]


class Escalate(ToolArgs):
    """Arguments for ``escalate``."""

    ticket_id: TicketId
    severity: Severity
    summary: Annotated[str, Field(min_length=1, max_length=4_000)]
    recommended_actions: list[RecommendedAction] = Field(default_factory=list, max_length=20)


class EntityRef(ToolArgs):
    """A host, account or indicator, as the report names it."""

    kind: EntityKind
    id: Annotated[str, Field(pattern=ENTITY_PATTERN, max_length=253)]


class IndicatorRef(ToolArgs):
    """An indicator, as the report lists it."""

    kind: IndicatorKind
    value: Annotated[str, Field(min_length=1, max_length=253)]


class SubmitReport(ToolArgs):
    """Arguments for ``submit_report``. ``narrative`` is free text and is not graded."""

    ticket_id: TicketId
    verdict: Verdict
    incident_type: IncidentType
    root_cause: EntityRef | None = Field(
        default=None, description="Where the incident started; null for a benign verdict."
    )
    affected_hosts: list[HostId] = Field(default_factory=list, max_length=50)
    affected_accounts: list[UserId] = Field(default_factory=list, max_length=50)
    indicators: list[IndicatorRef] = Field(default_factory=list, max_length=50)
    first_activity_at: AwareDatetime | None = Field(
        default=None, description="When the incident's first activity happened."
    )
    narrative: Annotated[str, Field(max_length=8_000)] = ""


class AskUser(ToolArgs):
    """Arguments for ``ask_user``."""

    question: Annotated[str, Field(min_length=1, max_length=1_000)]


__all__ = [
    "AccountAction",
    "AskUser",
    "BlockIndicator",
    "Escalate",
    "GetAlert",
    "GetHost",
    "GetUser",
    "HostAction",
    "ListAlerts",
    "Restore",
    "SearchLogs",
    "SubmitReport",
    "Timeline",
    "ToolArgs",
    "UpdateTicket",
]
