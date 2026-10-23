"""From a story to the rows each service loads.

:func:`expand` is a pure function of the story, the start time, the SLA table
and the seed: the same inputs give byte-identical rows. Ground truth is not in
its output. Graders read it from the story (decision 88).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kit.rng import SeededRNG

from ..services.common import iso
from ..vocab import ORDER, Severity
from .format import Artifact, Beat, Org, Story
from .noise import EVENT_FIELDS, Background, event, job_run_at

Row = dict[str, Any]

SCENARIOS_DIR = Path(__file__).parent
ORGS_DIR = SCENARIOS_DIR / "orgs"
STORIES_DIR = SCENARIOS_DIR / "stories"

CLEAN_HOST = [
    Artifact(
        kind="summary",
        detail=(
            "No unusual autostart entries, services, scheduled tasks or unsigned executables found."
        ),
    )
]
"""What ``collect_evidence`` finds on a host the story leaves untouched."""


# ------------------------------------------------------------------- loading
def load_org(name: str) -> Org:
    """A packaged company by name."""
    path = ORGS_DIR / f"{name}.yaml"
    if not path.is_file():
        msg = f"no company {name!r}; packaged: {sorted(p.stem for p in ORGS_DIR.glob('*.yaml'))}"
        raise ValueError(msg)
    return Org.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def story_ids() -> list[str]:
    """Every packaged story."""
    return sorted(path.stem for path in STORIES_DIR.glob("*.yaml"))


def load_story(story_id: str) -> Story:
    """A packaged story by id."""
    path = STORIES_DIR / f"{story_id}.yaml"
    if not path.is_file():
        msg = f"no story {story_id!r}; packaged: {story_ids()}"
        raise ValueError(msg)
    story = Story.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if story.id != story_id:
        msg = f"{path} holds story {story.id!r}"
        raise ValueError(msg)
    return story


def resolve_org(story: Story) -> Org:
    """The story's company: the packaged one with the story's changes applied."""
    org = load_org(story.org).model_dump()
    changes = story.org_changes
    for kind in ("hosts", "users"):
        rows: list[dict[str, Any]] = org[kind]
        for patch in getattr(changes, kind):
            target = next((row for row in rows if row["id"] == patch.get("id")), None)
            if target is None:
                rows.append(dict(patch))
            else:
                target.update(patch)
    org["changes"] += [change.model_dump() for change in changes.changes]
    org["jobs"] += [job.model_dump() for job in changes.jobs]
    org["business_indicators"] += [row.model_dump() for row in changes.business_indicators]
    resolved = Org.model_validate(org)
    check_story(story, resolved)
    return resolved


def check_story(story: Story, org: Org) -> None:
    """Every host and account the story names exists in its company."""
    hosts = {host.id for host in org.hosts}
    users = {user.id for user in org.users}
    problems: list[str] = []

    def need(kind: str, value: str | None, where: str) -> None:
        if value is not None and value not in (hosts if kind == "host" else users):
            problems.append(f"{where}: unknown {kind} {value}")

    for beat in story.beats:
        need("host", beat.host, f"beat {beat.key}")
        need("user", beat.user, f"beat {beat.key}")
        if beat.replaces_job is not None and not any(j.id == beat.replaces_job for j in org.jobs):
            problems.append(f"beat {beat.key}: unknown job {beat.replaces_job}")
        if beat.unless is not None:
            need("host", beat.unless.isolated, f"beat {beat.key}")
            need("user", beat.unless.disabled, f"beat {beat.key}")
    for alert in story.alerts:
        need("host", alert.host, f"alert {alert.id}")
        need("user", alert.user, f"alert {alert.id}")
    for host in story.local_evidence:
        need("host", host, "local_evidence")
    for prior in story.prior_actions:
        if prior.action.value in ("isolate_host", "reimage_host", "collect_evidence"):
            need("host", prior.target, "prior_actions")
        elif prior.action.value in ("disable_account", "reset_credentials"):
            need("user", prior.target, "prior_actions")
    truth = story.ground_truth
    for host in (*truth.affected_hosts, *truth.evidence_hosts):
        need("host", host, "ground_truth")
    for user in truth.affected_accounts:
        need("user", user, "ground_truth")
    if problems:
        raise ValueError(f"story {story.id}: " + "; ".join(problems))


# ----------------------------------------------------------------- expanding
@dataclass(slots=True)
class Expanded:
    """Each service's rows for one story, one start time and one seed."""

    org: Org
    started_at: dt.datetime
    events: list[Row] = field(default_factory=list)
    future_events: list[Row] = field(default_factory=list)
    """``{"at", "beat", "event", "unless"}``: delivered on the clock."""
    alerts: list[Row] = field(default_factory=list)
    future_alerts: list[Row] = field(default_factory=list)
    prior_actions: list[Row] = field(default_factory=list)
    tickets: list[Row] = field(default_factory=list)
    comments: list[Row] = field(default_factory=list)
    evidence: list[Row] = field(default_factory=list)


def expand(
    story: Story,
    start: dt.datetime,
    rng: SeededRNG,
    sla_minutes: dict[Severity, int],
) -> Expanded:
    """Turn a story into every service's rows."""
    org = resolve_org(story)
    out = Expanded(org=org, started_at=start)

    def at(offset: int) -> dt.datetime:
        return start + dt.timedelta(seconds=offset)

    skip: set[tuple[str, dt.datetime]] = set()
    for beat in story.beats:
        if beat.replaces_job is not None:
            for _, offset in beat.occurrences:
                if not job_run_at(org, beat.replaces_job, at(offset)):
                    msg = f"beat {beat.key}: {beat.replaces_job} does not run at {iso(at(offset))}"
                    raise ValueError(msg)
                skip.add((beat.replaces_job, at(offset)))
    background = Background(org, start, story.noise, rng.child("noise"), skip).generate()

    ids: dict[str, str] = {}
    story_rows: list[Row] = []
    for beat in story.beats:
        for key, offset in beat.occurrences:
            event_id = f"evt_{rng.child(f'beat/{key}').token(10)}"
            ids[key] = event_id
            row = _beat_row(beat, event_id, at(offset))
            if offset > 0:
                unless = beat.unless.model_dump(exclude_none=True) if beat.unless else None
                out.future_events.append(
                    {"at": iso(at(offset)), "beat": key, "event": row, "unless": unless}
                )
            else:
                story_rows.append(row)
    dropped = {source.value for source in story.dropped_sources}
    background = [row for row in background if row["source"] not in dropped]
    story_rows = [row for row in story_rows if row["source"] not in dropped]
    out.future_events = [r for r in out.future_events if r["event"]["source"] not in dropped]
    out.events = sorted([*background, *story_rows], key=lambda row: (row["ts"], row["id"]))
    _unique_ids([row["id"] for row in out.events] + [r["event"]["id"] for r in out.future_events])

    kept = {row["id"] for row in out.events} | {r["event"]["id"] for r in out.future_events}
    opened: dict[str, list[Row]] = {}
    for alert in story.alerts:
        row = {
            "id": alert.id,
            "created_at": iso(at(alert.at)),
            "rule": alert.rule,
            "severity": alert.severity.value,
            "description": alert.description,
            "host": alert.host,
            "user": alert.user,
            "ip": alert.ip,
            "domain": alert.domain,
            "event_ids": [ids[key] for key in alert.events if ids[key] in kept],
            "ticket_id": alert.ticket,
        }
        if alert.at > 0:
            unless = alert.unless.model_dump(exclude_none=True) if alert.unless else None
            out.future_alerts.append({"at": row["created_at"], "alert": row, "unless": unless})
        else:
            out.alerts.append(row)
            opened.setdefault(alert.ticket, []).append(row)
    out.alerts.sort(key=lambda row: (row["created_at"], row["id"]))

    for ticket in story.tickets:
        alerts = opened[ticket.id]
        created = min(row["created_at"] for row in alerts)
        severity = max((Severity(row["severity"]) for row in alerts), key=ORDER.__getitem__)
        due = dt.datetime.fromisoformat(created) + dt.timedelta(minutes=sla_minutes[severity])
        settled = ticket.escalated_at is not None or ticket.status == "closed"
        breached = not settled and due <= start
        if breached != ticket.breached_at_load and not settled:
            state = "has run out" if breached else "has not run out"
            msg = (
                f"ticket {ticket.id}: its SLA {state} when the agent starts ({iso(due)}); "
                "set breached_at_load to say whether the task means that"
            )
            raise ValueError(msg)
        out.tickets.append(
            {
                "id": ticket.id,
                "title": ticket.title,
                "created_at": created,
                "severity": severity.value,
                "status": ticket.status,
                "classification": ticket.classification.value if ticket.classification else None,
                "escalated_at": None
                if ticket.escalated_at is None
                else iso(at(ticket.escalated_at)),
                "sla_due_at": iso(due),
            }
        )
        for n, comment in enumerate(ticket.comments, start=1):
            out.comments.append(
                {
                    "ticket_id": ticket.id,
                    "at": iso(at(comment.at)),
                    "author": comment.author,
                    "body": comment.body,
                    "seq": n,
                }
            )

    for prior in story.prior_actions:
        host, user = org.host(prior.target), org.user(prior.target)
        entity = host or user
        out.prior_actions.append(
            {
                "action": prior.action.value,
                "target": prior.target,
                "target_kind": "host" if host else "user" if user else "indicator",
                "at": iso(at(prior.at)),
                "reason": prior.reason,
                "actor": prior.actor,
                "criticality": None if entity is None else entity.criticality.value,
            }
        )
    for host in org.hosts:
        artifacts = story.local_evidence.get(host.id, CLEAN_HOST)
        out.evidence.append({"host": host.id, "artifacts": [a.model_dump() for a in artifacts]})
    return out


def _beat_row(beat: Beat, event_id: str, moment: dt.datetime) -> Row:
    fields = {name: getattr(beat, name) for name in EVENT_FIELDS}
    return event(event_id, moment, beat.source.value, **fields)


def _unique_ids(values: list[str]) -> None:
    if len(set(values)) != len(values):
        msg = "two telemetry events drew the same id; change the seed or a beat key"
        raise ValueError(msg)


__all__ = [
    "CLEAN_HOST",
    "ORGS_DIR",
    "STORIES_DIR",
    "Expanded",
    "check_story",
    "expand",
    "load_org",
    "load_story",
    "resolve_org",
    "story_ids",
]
