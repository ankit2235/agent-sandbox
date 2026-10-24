"""The scenario format: a company, a synthetic incident story, and its ground truth.

A story is observable evidence only: log lines (``beats``), the alerts a SIEM
raised on them, the tickets the alerts were grouped into, and the artifacts on
hosts that only ``collect_evidence`` reaches. Its ``ground_truth`` is the answer
key. It never reaches a service: graders read it from the task (decision 88).

Times are offsets from when the agent starts: ``-2h10m`` is two hours and ten
minutes before, ``+25m`` is during the episode. A beat in the future is
delivered on the simulated clock, and one with ``unless`` is dropped if the
agent has contained what it names by then.

Every address must be an RFC 5737 documentation address and every domain an
RFC 2606 name; a story or company that breaks this does not load.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from kit.types import Strict

from ..vocab import (
    ALERT_PATTERN,
    CHANGE_PATTERN,
    DOMAIN_PATTERN,
    HOST_PATTERN,
    IP_PATTERN,
    KEY_PATTERN,
    PROCESS_PATTERN,
    SHA256_PATTERN,
    TICKET_PATTERN,
    USER_PATTERN,
    Action,
    Classification,
    Criticality,
    EntityKind,
    IncidentType,
    IndicatorKind,
    Severity,
    Source,
    Verdict,
    is_documentation_ip,
    is_example_domain,
)

OFFSET_PATTERN = r"^[+-]?(([0-9]+d)?([0-9]+h)?([0-9]+m)?([0-9]+s)?|[0-9]+)$"
_UNITS = {"d": 86_400, "h": 3_600, "m": 60, "s": 1}


def parse_offset(value: Any) -> Any:
    """``"-2h10m"`` → ``-7800``; integers are seconds already."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return value
    if isinstance(value, int):
        return value
    text = value.strip()
    if not re.match(OFFSET_PATTERN, text) or text.lstrip("+-") == "":
        msg = f"{value!r} is not an offset like '-2h10m' or '+25m'"
        raise ValueError(msg)
    sign = -1 if text.startswith("-") else 1
    if text.lstrip("+-").isdigit():
        return sign * int(text.lstrip("+-"))
    seconds = sum(int(n) * _UNITS[unit] for n, unit in re.findall(r"([0-9]+)([dhms])", text))
    return sign * seconds


Offset = Annotated[
    StrictInt,
    BeforeValidator(parse_offset),
    WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "string", "pattern": OFFSET_PATTERN}]}),
]
"""Seconds from the agent's start, written as an integer or ``-2h10m``."""

HostId = Annotated[str, Field(pattern=HOST_PATTERN)]
UserId = Annotated[str, Field(pattern=USER_PATTERN)]
AlertId = Annotated[str, Field(pattern=ALERT_PATTERN)]
TicketId = Annotated[str, Field(pattern=TICKET_PATTERN)]
ChangeId = Annotated[str, Field(pattern=CHANGE_PATTERN)]
Key = Annotated[str, Field(pattern=KEY_PATTERN)]
Ip = Annotated[str, Field(pattern=IP_PATTERN)]
Domain = Annotated[str, Field(pattern=DOMAIN_PATTERN, max_length=253)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
Process = Annotated[str, Field(pattern=PROCESS_PATTERN)]
Text = Annotated[str, Field(min_length=1, max_length=2_000)]
Port = Annotated[StrictInt, Field(ge=1, le=65_535)]


def _documentation_ip(value: str | None) -> str | None:
    if value is not None and not is_documentation_ip(value):
        msg = f"{value} is not an RFC 5737 documentation address"
        raise ValueError(msg)
    return value


def _example_domain(value: str | None) -> str | None:
    if value is not None and not is_example_domain(value):
        msg = f"{value} is not an RFC 2606 example domain"
        raise ValueError(msg)
    return value


# ---------------------------------------------------------------- the company
class Host(Strict):
    """A machine the company owns."""

    id: HostId
    ip: Ip
    role: Text
    os: Text
    owner: UserId | None = None
    criticality: Criticality
    location: Text = "Pune office"
    notes: str = ""

    _ip = field_validator("ip")(_documentation_ip)


class Baseline(Strict):
    """What normal looks like for an account."""

    locations: list[Text] = Field(min_length=1)
    hours_utc: tuple[StrictInt, StrictInt] = (3, 13)
    """Usual working hours, start inclusive and end exclusive, in UTC."""


class Travel(Strict):
    """An approved trip, on the account's record."""

    location: Text
    start: Offset
    end: Offset
    approved_by: UserId


class User(Strict):
    """An account: a person or a service."""

    id: UserId
    name: Text
    title: Text
    department: Text
    kind: Literal["human", "service"] = "human"
    groups: list[Key] = Field(default_factory=list)
    privileged: StrictBool = False
    criticality: Criticality = Severity.LOW
    workstation: HostId | None = None
    baseline: Baseline
    travel: list[Travel] = Field(default_factory=list)


class Change(Strict):
    """An approved change record: who may do what, where, when."""

    id: ChangeId
    title: Text
    actor: UserId
    hosts: list[HostId] = Field(default_factory=list)
    start: Offset
    end: Offset
    approved_by: UserId


class Job(Strict):
    """A scheduled job: background telemetry at fixed times, every day."""

    id: Key
    title: Text
    host: HostId
    user: UserId
    process: Process
    every_hours: Annotated[StrictInt, Field(ge=1, le=24)]
    minute_of_day: Annotated[StrictInt, Field(ge=0, lt=1_440)]
    """When the first run of the day starts, in UTC minutes after midnight."""
    dst_ip: Ip
    domain: Domain
    dst_port: Port = 443
    bytes_out: Annotated[StrictInt, Field(gt=0)]

    _ip = field_validator("dst_ip")(_documentation_ip)
    _domain = field_validator("domain")(_example_domain)


class BusinessIndicator(Strict):
    """An external address or domain the business depends on: blocking it hurts."""

    kind: IndicatorKind
    value: str
    purpose: Text
    criticality: Criticality

    @model_validator(mode="after")
    def _reserved(self) -> BusinessIndicator:
        if self.kind == IndicatorKind.IP:
            _documentation_ip(self.value)
        elif self.kind == IndicatorKind.DOMAIN:
            _example_domain(self.value)
        return self


class Site(Strict):
    """Where the office is and how it reaches the internet."""

    location: Text
    egress_ip: Ip

    _ip = field_validator("egress_ip")(_documentation_ip)


class CommonDomain(Strict):
    """A domain everyone uses, for background DNS and web traffic."""

    domain: Domain
    ip: Ip

    _ip = field_validator("ip")(_documentation_ip)
    _domain = field_validator("domain")(_example_domain)


class Org(Strict):
    """The simulated company."""

    name: Text
    domain: Domain
    office: Site
    hosts: list[Host]
    users: list[User]
    changes: list[Change] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    business_indicators: list[BusinessIndicator] = Field(default_factory=list)
    common_domains: list[CommonDomain] = Field(default_factory=list)

    _domain = field_validator("domain")(_example_domain)

    @model_validator(mode="after")
    def _consistent(self) -> Org:
        hosts = {host.id for host in self.hosts}
        users = {user.id for user in self.users}
        _unique("host", [host.id for host in self.hosts])
        _unique("user", [user.id for user in self.users])
        _unique("host address", [host.ip for host in self.hosts])
        problems = [
            f"host {h.id}: unknown owner {h.owner}"
            for h in self.hosts
            if h.owner and h.owner not in users
        ]
        for user in self.users:
            if user.workstation is not None and user.workstation not in hosts:
                problems.append(f"user {user.id}: unknown workstation {user.workstation}")
            problems += [
                f"user {user.id}: travel approver {t.approved_by} unknown"
                for t in user.travel
                if t.approved_by not in users
            ]
        for change in self.changes:
            problems += [f"{change.id}: unknown host {h}" for h in change.hosts if h not in hosts]
            problems += [
                f"{change.id}: unknown account {u}"
                for u in (change.actor, change.approved_by)
                if u not in users
            ]
        for job in self.jobs:
            if job.host not in hosts or job.user not in users:
                problems.append(f"job {job.id}: unknown host or account")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def host(self, host_id: str) -> Host | None:
        """A host by id."""
        return next((host for host in self.hosts if host.id == host_id), None)

    def user(self, user_id: str) -> User | None:
        """An account by id."""
        return next((user for user in self.users if user.id == user_id), None)


class OrgChanges(Strict):
    """A story's edits to a company: rows to add, or to update by ``id``."""

    hosts: list[dict[str, Any]] = Field(default_factory=list)
    users: list[dict[str, Any]] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    business_indicators: list[BusinessIndicator] = Field(default_factory=list)


# ------------------------------------------------------------------ the story
class Condition(Strict):
    """What stops a future beat or alert from happening: exactly one containment."""

    isolated: HostId | None = None
    disabled: UserId | None = None
    blocked: str | None = None
    reset: UserId | None = None
    """The account's credentials were reset (or it was disabled) before this falls due."""

    @model_validator(mode="after")
    def _exactly_one(self) -> Condition:
        given = (self.isolated, self.disabled, self.blocked, self.reset)
        if sum(value is not None for value in given) != 1:
            msg = "give exactly one of isolated, disabled, blocked, reset"
            raise ValueError(msg)
        return self


class Repeat(Strict):
    """A beat that happens again every ``every`` seconds, ``times`` times in all."""

    every: Annotated[Offset, Field(gt=0)]
    times: Annotated[StrictInt, Field(ge=2, le=200)]


class Beat(Strict):
    """One log line of the story, as a log source would record it."""

    key: Key
    at: Offset
    source: Source
    host: HostId | None = None
    user: UserId | None = None
    src_ip: Ip | None = None
    dst_ip: Ip | None = None
    dst_port: Port | None = None
    domain: Domain | None = None
    process: Process | None = None
    parent_process: Process | None = None
    action: Key | None = None
    outcome: Literal["success", "failure", "blocked"] | None = None
    bytes_out: Annotated[StrictInt, Field(ge=0)] | None = None
    geo: Text | None = None
    app: Key | None = None
    sha256: Sha256 | None = None
    message: str = ""
    unless: Condition | None = None
    """Future occurrences only: dropped if this containment is in force when they fall due."""
    harmful: StrictBool = False
    """Grader-only: if this happens, containment came too late."""
    repeat: Repeat | None = None
    replaces_job: Key | None = None
    """This beat is that job's run at the same time: the background run is left out."""

    _ips = field_validator("src_ip", "dst_ip")(_documentation_ip)
    _domain = field_validator("domain")(_example_domain)

    @model_validator(mode="after")
    def _future_rules(self) -> Beat:
        if self.harmful and (self.unless is None or self.last_at <= 0):
            msg = f"beat {self.key}: a harmful beat must be in the future and have 'unless'"
            raise ValueError(msg)
        if self.unless is not None and self.last_at <= 0:
            msg = f"beat {self.key}: 'unless' only applies to the future"
            raise ValueError(msg)
        return self

    @property
    def occurrences(self) -> list[tuple[str, int]]:
        """``(key, at)`` for every time this beat happens; repeats are ``key#n``."""
        if self.repeat is None:
            return [(self.key, self.at)]
        return [
            (f"{self.key}#{n}", self.at + (n - 1) * self.repeat.every)
            for n in range(1, self.repeat.times + 1)
        ]

    @property
    def last_at(self) -> int:
        """When the last occurrence happens."""
        return self.occurrences[-1][1]


class AlertDef(Strict):
    """An alert the SIEM raised, linked to the beats it fired on."""

    id: AlertId
    at: Offset
    rule: Text
    severity: Severity
    description: Text
    host: HostId | None = None
    user: UserId | None = None
    ip: Ip | None = None
    domain: Domain | None = None
    events: list[str] = Field(default_factory=list)
    """Beat keys (``key#n`` for one occurrence of a repeated beat)."""
    ticket: TicketId
    unless: Condition | None = None

    _ip = field_validator("ip")(_documentation_ip)
    _domain = field_validator("domain")(_example_domain)

    @model_validator(mode="after")
    def _future_rules(self) -> AlertDef:
        if self.unless is not None and self.at <= 0:
            msg = f"alert {self.id}: 'unless' only applies to the future"
            raise ValueError(msg)
        return self


class CommentDef(Strict):
    """A comment already on a ticket."""

    at: Offset
    author: Text
    body: Text

    @field_validator("at")
    @classmethod
    def _past(cls, value: int) -> int:
        if value > 0:
            msg = "a seeded comment must be in the past"
            raise ValueError(msg)
        return value


class TicketDef(Strict):
    """An incident ticket the alert pipeline opened, possibly already worked on."""

    id: TicketId
    title: Text
    classification: Classification | None = None
    status: Literal["open", "closed"] = "open"
    escalated_at: Offset | None = None
    comments: list[CommentDef] = Field(default_factory=list)
    breached_at_load: StrictBool = False
    """The task means this ticket's SLA to have run out before the agent starts."""

    @model_validator(mode="after")
    def _consistent(self) -> TicketDef:
        if self.status == "closed" and self.classification is None:
            msg = f"ticket {self.id}: a closed ticket needs a classification"
            raise ValueError(msg)
        if self.escalated_at is not None and self.escalated_at > 0:
            msg = f"ticket {self.id}: escalated_at must be in the past"
            raise ValueError(msg)
        return self


class PriorAction(Strict):
    """Containment a previous shift already took, still on the record when the agent starts."""

    action: Action
    target: str = Field(min_length=1, max_length=253)
    at: Offset
    reason: Text
    actor: Text = "previous shift"

    @model_validator(mode="after")
    def _past(self) -> PriorAction:
        if self.at > 0:
            msg = "a prior action must be in the past"
            raise ValueError(msg)
        return self


class MisleadingNote(Strict):
    """Grader-only record of an instruction planted in the world by a stress modifier.

    The note itself is in a ticket comment or a log message; this says which ticket it
    wanted closed and which healthy account or host it wanted disrupted.
    """

    ticket: TicketId
    target: str = Field(min_length=1, max_length=253)
    placement: Literal["comment", "log"]


class Artifact(Strict):
    """One item of local evidence on a host."""

    kind: Key
    detail: Text


# --------------------------------------------------------------- ground truth
class ActionRef(Strict):
    """A containment action on one target."""

    action: Action
    target: str = Field(min_length=1, max_length=253)


class RequiredAction(ActionRef):
    """A containment the incident needs; any one of ``alternatives`` also does."""

    alternatives: list[ActionRef] = Field(default_factory=list)

    @property
    def options(self) -> list[ActionRef]:
        """The action itself and its alternatives."""
        return [ActionRef(action=self.action, target=self.target), *self.alternatives]


class ContainmentTruth(Strict):
    """The minimal containment, plus actions that are fine but not needed."""

    required: list[RequiredAction] = Field(default_factory=list)
    acceptable: list[ActionRef] = Field(default_factory=list)


class EntityRef(Strict):
    """A host, account or indicator, as a report names it."""

    kind: EntityKind
    id: str = Field(min_length=1, max_length=253)


class IndicatorTruth(Strict):
    """An indicator of the incident; ``required: false`` may be reported but need not be."""

    kind: IndicatorKind
    value: str = Field(min_length=1, max_length=253)
    required: StrictBool = True


TicketTruth = Literal["true_positive", "benign_positive", "false_positive"]


class GroundTruth(Strict):
    """The answer key a SOC reviewer signs off."""

    tickets: dict[TicketId, TicketTruth]
    verdict: Verdict
    incident_type: IncidentType
    root_cause: EntityRef | None = None
    root_cause_alternatives: list[EntityRef] = Field(default_factory=list)
    """Other answers a reviewer accepts, where the starting point is honestly ambiguous."""
    affected_hosts: list[HostId] = Field(default_factory=list)
    affected_accounts: list[UserId] = Field(default_factory=list)
    indicators: list[IndicatorTruth] = Field(default_factory=list)
    containment: ContainmentTruth = Field(default_factory=ContainmentTruth)
    escalate: list[TicketId] = Field(default_factory=list)
    """Tickets that must be escalated before their SLA runs out."""
    first_activity_at: Offset | None = None
    first_activity_tolerance_minutes: Annotated[StrictInt, Field(ge=0, le=120)] = 5
    evidence_hosts: list[HostId] = Field(default_factory=list)
    """Hosts whose local evidence matters: it must be collected before any reimage."""

    @model_validator(mode="after")
    def _consistent(self) -> GroundTruth:
        positive = {t for t, c in self.tickets.items() if c == "true_positive"}
        if self.verdict == Verdict.BENIGN:
            if (
                positive
                or self.affected_hosts
                or self.affected_accounts
                or self.containment.required
            ):
                msg = "a benign story has no true positive, no affected entity, no containment"
                raise ValueError(msg)
        elif not positive:
            msg = "a malicious story needs at least one true-positive ticket"
            raise ValueError(msg)
        if not set(self.escalate) <= positive:
            msg = f"only true positives are escalated, not {sorted(set(self.escalate) - positive)}"
            raise ValueError(msg)
        return self

    @property
    def indicator_values(self) -> set[str]:
        """Every indicator value, required or not."""
        return {indicator.value for indicator in self.indicators}


class NoiseConfig(Strict):
    """How much background telemetry to generate, and for whom not."""

    window_hours: Annotated[StrictInt, Field(ge=1, le=72)] = 24
    quiet: list[str] = Field(default_factory=list)
    """Accounts and hosts with no background activity: the story writes all of theirs."""


class Story(Strict):
    """One scenario: the company, what happened, what the SIEM saw, and the truth."""

    id: Key
    title: Text
    summary: Text
    """For reviewers: the story in plain words. Never shown to the agent."""
    org: Key = "default"
    org_changes: OrgChanges = Field(default_factory=OrgChanges)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)
    beats: list[Beat] = Field(default_factory=list)
    alerts: list[AlertDef] = Field(min_length=1)
    tickets: list[TicketDef] = Field(min_length=1)
    local_evidence: dict[HostId, list[Artifact]] = Field(default_factory=dict)
    prior_actions: list[PriorAction] = Field(default_factory=list)
    optional_sources: list[Source] = Field(default_factory=list)
    """Log sources the story can be solved without: the only ones partial_telemetry may drop."""
    dropped_sources: list[Source] = Field(default_factory=list)
    """Log sources missing in this run (set by the partial_telemetry modifier)."""
    misleading: list[MisleadingNote] = Field(default_factory=list)
    ground_truth: GroundTruth

    @model_validator(mode="after")
    def _references(self) -> Story:
        occurrences = [key for beat in self.beats for key, _ in beat.occurrences]
        _unique("beat key", [beat.key for beat in self.beats])
        _unique("alert", [alert.id for alert in self.alerts])
        _unique("ticket", [ticket.id for ticket in self.tickets])
        known = set(occurrences) | {beat.key for beat in self.beats if beat.repeat is None}
        tickets = {ticket.id for ticket in self.tickets}
        problems: list[str] = []
        for alert in self.alerts:
            problems += [
                f"alert {alert.id}: unknown beat {e}" for e in alert.events if e not in known
            ]
            if alert.ticket not in tickets:
                problems.append(f"alert {alert.id}: unknown ticket {alert.ticket}")
        opened = {alert.ticket for alert in self.alerts if alert.at <= 0}
        problems += [f"ticket {t}: no alert before the start" for t in sorted(tickets - opened)]
        if set(self.ground_truth.tickets) != tickets:
            problems.append("ground_truth.tickets must classify exactly the story's tickets")
        if not set(self.dropped_sources) <= set(self.optional_sources):
            problems.append("only optional sources may be dropped")
        truth = self.ground_truth
        touched = {*truth.affected_hosts, *truth.affected_accounts, *truth.indicator_values}
        for note in self.misleading:
            if note.target in touched:
                problems.append(f"misleading note target {note.target} is not healthy")
            if note.ticket not in tickets:
                problems.append(f"misleading note on unknown ticket {note.ticket}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _unique(what: str, values: list[str]) -> None:
    repeated = sorted(value for value, count in Counter(values).items() if count > 1)
    if repeated:
        msg = f"duplicate {what}: {repeated}"
        raise ValueError(msg)


__all__ = [
    "OFFSET_PATTERN",
    "ActionRef",
    "AlertDef",
    "Artifact",
    "Baseline",
    "Beat",
    "BusinessIndicator",
    "Change",
    "CommentDef",
    "CommonDomain",
    "Condition",
    "ContainmentTruth",
    "EntityRef",
    "GroundTruth",
    "Host",
    "IndicatorTruth",
    "Job",
    "MisleadingNote",
    "NoiseConfig",
    "Offset",
    "Org",
    "OrgChanges",
    "PriorAction",
    "Repeat",
    "RequiredAction",
    "Site",
    "Story",
    "TicketDef",
    "TicketTruth",
    "Travel",
    "User",
    "parse_offset",
]
