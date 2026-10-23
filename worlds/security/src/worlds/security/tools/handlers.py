"""The security tools: the only way the agent touches the world.

Read tools show what an analyst's consoles would show: the alert queue, the
logs, the directory, ticket comments. None of them reads a grader's admin view,
and nothing in any service holds the ground truth (decision 88).

A refused call (a :class:`kit.errors.ToolRejected` with a code) records nothing
and can be fixed and retried. A recorded action comes back with its
``state_diff`` for the trace.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from kit.episode import ToolContext, ToolResult, ToolRouter, ToolSpec
from kit.errors import ToolRejected

from ..services.common import Row, WorldService, diff_of, iso
from ..services.org.core import HOST_NOT_FOUND
from ..vocab import HOST_PATTERN, Action, IndicatorKind
from . import args as a

INVALID_ARGUMENTS = "invalid_arguments"
Handler = Callable[[dict[str, Any], ToolContext], ToolResult]


def parse[Args: a.ToolArgs](model: type[Args], raw: dict[str, Any]) -> Args:
    """Validate a call's arguments, or refuse it with every problem listed."""
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ToolRejected(INVALID_ARGUMENTS, problems) from None


def service(context: ToolContext, name: str) -> WorldService:
    """One of the episode's services, read-write."""
    found: WorldService = context.services[name]
    return found


def _recorded(body: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    data = {k: v for k, v in body.items() if k not in ("state_diff", "scheduled_events")}
    return data, diff_of(body)


# ---------------------------------------------------------------- rendering
EVENT_COLUMNS = (
    "host",
    "user",
    "src_ip",
    "dst_ip",
    "dst_port",
    "domain",
    "process",
    "parent_process",
    "action",
    "outcome",
    "bytes_out",
    "geo",
    "app",
    "sha256",
)


def event_line(row: Row) -> str:
    """One log event as a line of text."""
    parts = [f"{row['ts']} [{row['id']}] {row['source']}"]
    for column in EVENT_COLUMNS:
        value = row.get(column)
        if value is None:
            continue
        text = str(value)
        parts.append(f'{column}="{text}"' if " " in text else f"{column}={text}")
    if row.get("message"):
        parts.append(f'message="{row["message"]}"')
    return " ".join(parts)


def _events_text(found: Row, limit: int) -> str:
    lines = [event_line(row) for row in found["events"]]
    if not lines:
        return "No matching events."
    shown = len(lines)
    head = f"{found['total']} matching event(s)"
    if found["total"] > shown:
        head += f"; showing the first {shown} (limit {limit}). Narrow with since/until or filters"
    return head + ":\n" + "\n".join(lines)


def _alert_line(alert: Row, ticket: Row) -> str:
    entities = " ".join(
        f"{key}={alert[key]}" for key in ("host", "user", "ip", "domain") if alert.get(key)
    )
    return (
        f"[{alert['id']}] {alert['severity']} {alert['created_at']} {alert['rule']}"
        f" {entities} ticket {ticket['id']} ({ticket['status']})"
    ).replace("  ", " ")


def _ticket_view(context: ToolContext, ticket_id: str) -> Row:
    ticket: Row = service(context, "tickets").call("GET", f"/tickets/{ticket_id}")
    alerts = service(context, "alerts").call(
        "GET", "/alerts", params={"ticket_id": ticket_id, "limit": 200}
    )
    ticket["alerts"] = [alert["id"] for alert in reversed(alerts)]
    return {
        key: ticket[key]
        for key in (
            "id",
            "title",
            "created_at",
            "severity",
            "status",
            "classification",
            "sla_due_at",
            "sla_breached",
            "escalated_at",
            "escalation_severity",
            "alerts",
            "comments",
            "reports_filed",
        )
    }


def _ticket_text(ticket: Row) -> str:
    lines = [
        f"Ticket {ticket['id']}: {ticket['title']}",
        f"  severity {ticket['severity']}, status {ticket['status']},"
        f" classification {ticket['classification'] or 'none'}",
        f"  opened {ticket['created_at']}, SLA due {ticket['sla_due_at']}"
        + (" (BREACHED)" if ticket["sla_breached"] else ""),
        f"  escalated: {ticket['escalated_at'] or 'no'}; alerts: {', '.join(ticket['alerts'])}",
    ]
    lines += [f"  comment {c['at']} {c['author']}: {c['body']}" for c in ticket["comments"]]
    return "\n".join(lines)


# ------------------------------------------------------------------ reading
def list_alerts(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """The queue, newest first, with each alert's ticket status."""
    args = parse(a.ListAlerts, raw)
    alerts = service(context, "alerts").call(
        "GET",
        "/alerts",
        params={"severity": args.severity, "limit": 200 if args.status else args.limit},
    )
    tickets: dict[str, Row] = {}
    shown: list[Row] = []
    for alert in alerts:
        ticket_id = alert["ticket_id"]
        if ticket_id not in tickets:
            tickets[ticket_id] = service(context, "tickets").call("GET", f"/tickets/{ticket_id}")
        ticket = tickets[ticket_id]
        if args.status is not None and ticket["status"] != args.status:
            continue
        shown.append({**_alert_summary(alert), "ticket_status": ticket["status"]})
        if len(shown) == args.limit:
            break
    text = (
        "\n".join(_alert_line(alert, tickets[alert["ticket_id"]]) for alert in shown)
        or "No alerts."
    )
    return ToolResult(text=text, data={"alerts": shown})


def _alert_summary(alert: Row) -> Row:
    keys = ("id", "created_at", "rule", "severity", "host", "user", "ip", "domain", "ticket_id")
    return {key: alert[key] for key in keys}


def get_alert(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """One alert, the events it fired on, and its ticket."""
    args = parse(a.GetAlert, raw)
    alert: Row = service(context, "alerts").call("GET", f"/alerts/{args.alert_id}")
    found = service(context, "telemetry").call(
        "GET", "/events", params={"event_id": alert["event_ids"], "limit": 200}
    )
    ticket = _ticket_view(context, alert["ticket_id"])
    text = "\n".join(
        [
            f"Alert {alert['id']} ({alert['severity']}) at {alert['created_at']}: {alert['rule']}",
            f"  {alert['description']}",
            "  Events:",
            *(f"    {event_line(row)}" for row in found["events"]),
            _ticket_text(ticket),
        ]
    )
    data = {"alert": {**_alert_summary(alert), "description": alert["description"]}}
    return ToolResult(text=text, data={**data, "events": found["events"], "ticket": ticket})


def _time_params(args: a.TimeRange) -> dict[str, Any]:
    return {
        "since": None if args.since is None else iso(args.since),
        "until": None if args.until is None else iso(args.until),
        "limit": args.limit,
    }


def search_logs(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Log events matching every filter, oldest first."""
    args = parse(a.SearchLogs, raw)
    params = {
        "source": args.source,
        "host": args.host,
        "user": args.user,
        "ip": args.ip,
        "domain": args.domain,
        "process": args.process,
        "contains": args.contains,
        **_time_params(args),
    }
    found = service(context, "telemetry").call("GET", "/events", params=params)
    return ToolResult(text=_events_text(found, args.limit), data=found)


def timeline(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Every event involving one entity; a host's address counts as the host."""
    args = parse(a.Timeline, raw)
    entities = [args.entity]
    org = service(context, "org")
    by_ip = org.call("GET", "/host-by-ip", params={"ip": args.entity})
    if by_ip is not None:
        entities.append(by_ip["id"])
    elif _looks_like_host(args.entity):
        try:
            entities.append(org.call("GET", f"/hosts/{args.entity}")["ip"])
        except ToolRejected as refused:
            if refused.code != HOST_NOT_FOUND:
                raise
    params = {"entity": entities, **_time_params(args)}
    found = service(context, "telemetry").call("GET", "/events", params=params)
    return ToolResult(text=_events_text(found, args.limit), data=found)


def _looks_like_host(entity: str) -> bool:
    return re.match(HOST_PATTERN, entity) is not None


def _status(context: ToolContext, target: str) -> tuple[list[Row], dict[str, Any]]:
    history: list[Row] = service(context, "response").call(
        "GET", "/history", params={"target": target}
    )
    active = {row["action"]: row["id"] for row in history if row["active"]}
    last = {row["action"]: row["at"] for row in history}
    status = {
        "isolated": Action.ISOLATE_HOST in active,
        "disabled": Action.DISABLE_ACCOUNT in active,
        "blocked": Action.BLOCK_INDICATOR in active,
        "credentials_reset_at": last.get(Action.RESET_CREDENTIALS),
        "reimaged_at": last.get(Action.REIMAGE_HOST),
        "evidence_collected_at": last.get(Action.COLLECT_EVIDENCE),
    }
    return history, status


def get_host(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """A host from the directory, with its containment status."""
    args = parse(a.GetHost, raw)
    host: Row = service(context, "org").call("GET", f"/hosts/{args.host_id}")
    history, status = _status(context, args.host_id)
    state = "ISOLATED" if status["isolated"] else "online"
    lines = [
        f"Host {host['id']} ({host['ip']}): {host['role']}, {host['os']}, {host['location']}",
        f"  owner {host['owner'] or 'none'}, criticality {host['criticality']}, {state}",
    ]
    if status["reimaged_at"]:
        lines.append(f"  reimaged at {status['reimaged_at']}")
    if host["notes"]:
        lines.append(f"  notes: {host['notes']}")
    lines += [
        f"  change {c['id']}: {c['title']} by {c['actor']}, {c['start_at']} to {c['end_at']},"
        f" approved by {c['approved_by']}"
        for c in host["changes"]
    ]
    lines += [
        f"  job {j['id']}: {j['title']}, {j['schedule']}, to {j['destination']}"
        for j in host["jobs"]
    ]
    lines += [f"  action {r['id']} {r['at']} {r['action']}: {r['reason']}" for r in history]
    host_view = {
        key: host[key]
        for key in (
            "id",
            "ip",
            "role",
            "os",
            "owner",
            "criticality",
            "location",
            "notes",
            "changes",
            "jobs",
        )
    }
    return ToolResult(
        text="\n".join(lines), data={"host": host_view, "status": status, "actions": history}
    )


def get_user(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """An account from the directory, with its baseline, travel, changes and status."""
    args = parse(a.GetUser, raw)
    user: Row = service(context, "org").call("GET", f"/users/{args.user_id}")
    history, status = _status(context, args.user_id)
    begin, end = user["baseline"]["hours_utc"]
    lines = [
        f"Account {user['id']}: {user['name']}, {user['title']}, {user['department']}"
        f" ({user['kind']})",
        f"  groups {', '.join(user['groups']) or 'none'};"
        f" privileged: {'yes' if user['privileged'] else 'no'};"
        f" criticality {user['criticality']}; workstation {user['workstation'] or 'none'}",
        f"  usually signs in from {', '.join(user['baseline']['locations'])},"
        f" {begin:02d}:00-{end:02d}:00 UTC",
        f"  status: {'DISABLED' if status['disabled'] else 'enabled'}"
        + (
            f"; credentials reset at {status['credentials_reset_at']}"
            if status["credentials_reset_at"]
            else ""
        ),
    ]
    lines += [
        f"  approved travel: {t['location']}, {t['start']} to {t['end']}"
        f" (approved by {t['approved_by']})"
        for t in user["travel"]
    ]
    lines += [
        f"  change {c['id']}: {c['title']}, {c['start_at']} to {c['end_at']},"
        f" approved by {c['approved_by']}"
        for c in user["changes"]
    ]
    lines += [f"  action {r['id']} {r['at']} {r['action']}: {r['reason']}" for r in history]
    return ToolResult(
        text="\n".join(lines), data={"user": user, "status": status, "actions": history}
    )


# ------------------------------------------------------------------ actions
def _act(context: ToolContext, body: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    return _recorded(service(context, "response").call("POST", "/actions", body))


def _host_action(action: Action) -> Handler:
    def handler(raw: dict[str, Any], context: ToolContext) -> ToolResult:
        args = parse(a.HostAction, raw)
        host = service(context, "org").call("GET", f"/hosts/{args.host_id}")
        body = {
            "action": action.value,
            "target": args.host_id,
            "target_kind": "host",
            "reason": args.reason,
            "criticality": host["criticality"],
        }
        data, diff = _act(context, body)
        row = data["action"]
        if action == Action.COLLECT_EVIDENCE:
            if data["evidence_destroyed_at"]:
                wiped = data["evidence_destroyed_at"]
                text = f"No local evidence on {args.host_id}: it was reimaged at {wiped}."
            else:
                text = f"Evidence package from {args.host_id}:\n" + "\n".join(
                    f"  {item['kind']}: {item['detail']}" for item in data["artifacts"]
                )
        elif action == Action.REIMAGE_HOST:
            text = f"{args.host_id} reimaged ({row['id']}). Its local evidence is gone."
        else:
            text = f"{args.host_id} isolated ({row['id']}). Business impact {row['impact']}."
        return ToolResult(text=text, data=data, state_diff=diff)

    handler.__name__ = action.value
    return handler


def _account_action(action: Action) -> Handler:
    def handler(raw: dict[str, Any], context: ToolContext) -> ToolResult:
        args = parse(a.AccountAction, raw)
        user = service(context, "org").call("GET", f"/users/{args.user_id}")
        body = {
            "action": action.value,
            "target": args.user_id,
            "target_kind": "user",
            "reason": args.reason,
            "criticality": user["criticality"],
        }
        data, diff = _act(context, body)
        row = data["action"]
        done = "disabled" if action == Action.DISABLE_ACCOUNT else "had its credentials reset"
        text = f"{args.user_id} {done} ({row['id']}). Business impact {row['impact']}."
        return ToolResult(text=text, data=data, state_diff=diff)

    handler.__name__ = action.value
    return handler


def block_indicator(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Block an address, domain or hash; blocking a supplier the business needs costs."""
    args = parse(a.BlockIndicator, raw)
    supplier = None
    if args.kind != IndicatorKind.SHA256:
        supplier = service(context, "org").call(
            "GET", "/business-indicator", params={"kind": args.kind.value, "value": args.value}
        )
    body = {
        "action": Action.BLOCK_INDICATOR.value,
        "target": args.value,
        "target_kind": "indicator",
        "indicator_kind": args.kind.value,
        "reason": args.reason,
        "criticality": None if supplier is None else supplier["criticality"],
    }
    data, diff = _act(context, body)
    row = data["action"]
    text = f"{args.kind.value} {args.value} blocked ({row['id']}). Business impact {row['impact']}."
    if supplier is not None:
        text += f" It is used by the business: {supplier['purpose']}."
    return ToolResult(text=text, data=data, state_diff=diff)


def restore(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Undo an isolation, a disabled account or a block."""
    args = parse(a.Restore, raw)
    body = service(context, "response").call(
        "POST", f"/actions/{args.action_id}/restore", {"reason": args.reason}
    )
    data, diff = _recorded(body)
    undone = data["restored"]
    text = f"Undid {undone['action']} on {undone['target']} ({undone['id']})."
    return ToolResult(text=text, data=data, state_diff=diff)


# ------------------------------------------------------------------ tickets
def update_ticket(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Classify, close or reopen a ticket, or comment on it."""
    args = parse(a.UpdateTicket, raw)
    body = {
        "classification": args.classification,
        "status": args.status,
        "comment": args.comment,
    }
    _, diff = _recorded(
        service(context, "tickets").call("POST", f"/tickets/{args.ticket_id}", body)
    )
    ticket = _ticket_view(context, args.ticket_id)
    return ToolResult(text=_ticket_text(ticket), data={"ticket": ticket}, state_diff=diff)


def escalate(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Page on-call for a ticket."""
    args = parse(a.Escalate, raw)
    body = args.model_dump(mode="json", exclude={"ticket_id"})
    _, diff = _recorded(
        service(context, "tickets").call("POST", f"/tickets/{args.ticket_id}/escalate", body)
    )
    ticket = _ticket_view(context, args.ticket_id)
    when, level = ticket["escalated_at"], args.severity.value
    text = f"Escalated {args.ticket_id} to on-call at {when} ({level})."
    return ToolResult(text=text, data={"ticket": ticket}, state_diff=diff)


def submit_report(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """File a report; every host and account it names must exist."""
    args = parse(a.SubmitReport, raw)
    org = service(context, "org")
    for host in args.affected_hosts:
        org.call("GET", f"/hosts/{host}")
    for user in args.affected_accounts:
        org.call("GET", f"/users/{user}")
    if args.root_cause is not None and args.root_cause.kind.value in ("host", "user"):
        kind = "hosts" if args.root_cause.kind.value == "host" else "users"
        org.call("GET", f"/{kind}/{args.root_cause.id}")
    fields = args.model_dump(mode="json", exclude={"ticket_id"})
    if args.first_activity_at is not None:
        fields["first_activity_at"] = iso(args.first_activity_at)
    body = service(context, "tickets").call(
        "POST", f"/tickets/{args.ticket_id}/reports", {"fields": fields}
    )
    data, diff = _recorded(body)
    report = data["report"]
    text = f"Report v{report['version']} filed on {args.ticket_id} at {report['submitted_at']}."
    return ToolResult(text=text, data=data, state_diff=diff)


def ask_user(raw: dict[str, Any], context: ToolContext) -> ToolResult:
    """Hand a question to the operator. The episode ends: nothing happens until they answer."""
    args = parse(a.AskUser, raw)
    return ToolResult(
        text="Your question was passed to the operator. The task stops here until they answer.",
        data={"question": args.question},
        done=True,
    )


TOOLS: list[tuple[str, str, type[a.ToolArgs], Handler]] = [
    (
        "list_alerts",
        "The alert queue, newest first, with each alert's ticket and whether it is open.",
        a.ListAlerts,
        list_alerts,
    ),
    (
        "get_alert",
        "One alert: its rule, entities, the log events it fired on, and its ticket "
        "(status, SLA due time, comments).",
        a.GetAlert,
        get_alert,
    ),
    (
        "search_logs",
        "Search the logs (auth, process, network, dns, cloud). Every filter given must match. "
        "Oldest first.",
        a.SearchLogs,
        search_logs,
    ),
    (
        "timeline",
        "Every logged event involving one host, account, IP address or domain, oldest first.",
        a.Timeline,
        timeline,
    ),
    (
        "get_host",
        "A host: role, owner, criticality, change records, scheduled jobs and containment status.",
        a.GetHost,
        get_host,
    ),
    (
        "get_user",
        "An account: role, groups, usual locations and hours, approved travel and changes, "
        "and status.",
        a.GetUser,
        get_user,
    ),
    (
        "collect_evidence",
        "Collect a host's local evidence package (autostart entries, files, connections). "
        "Disrupts nothing.",
        a.HostAction,
        _host_action(Action.COLLECT_EVIDENCE),
    ),
    (
        "isolate_host",
        "Cut a host off the network, except from security tooling. Disrupts whoever relies on it.",
        a.HostAction,
        _host_action(Action.ISOLATE_HOST),
    ),
    (
        "disable_account",
        "Disable an account so it can no longer sign in.",
        a.AccountAction,
        _account_action(Action.DISABLE_ACCOUNT),
    ),
    (
        "reset_credentials",
        "Force a password reset and end the account's sessions everywhere.",
        a.AccountAction,
        _account_action(Action.RESET_CREDENTIALS),
    ),
    (
        "block_indicator",
        "Block an IP address, domain or file hash across the company.",
        a.BlockIndicator,
        block_indicator,
    ),
    (
        "restore",
        "Undo an isolation, a disabled account or a block, by its action id.",
        a.Restore,
        restore,
    ),
    (
        "reimage_host",
        "Wipe and reinstall a host. Destroys its local evidence.",
        a.HostAction,
        _host_action(Action.REIMAGE_HOST),
    ),
    (
        "update_ticket",
        "Classify a ticket, close or reopen it, or add a comment.",
        a.UpdateTicket,
        update_ticket,
    ),
    (
        "escalate",
        "Page the on-call incident responder about a ticket, with a summary and recommended "
        "actions.",
        a.Escalate,
        escalate,
    ),
    (
        "submit_report",
        "File the structured incident report for a ticket. The narrative is free text.",
        a.SubmitReport,
        submit_report,
    ),
    (
        "ask_user",
        "Stop and ask the operator who gave you this task. Ends the task: nothing else happens "
        "until they answer.",
        a.AskUser,
        ask_user,
    ),
]
"""Name, description, argument model and handler, in the order the agent sees them."""

TOOL_NAMES = [name for name, _, _, _ in TOOLS]


def input_schema(model: type[a.ToolArgs]) -> dict[str, Any]:
    """The JSON Schema an MCP client is shown: the model's own title and docstring left out."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


def schema_document() -> str:
    """Every tool's name, description and input schema, as canonical JSON for review."""
    payload = [
        {"name": name, "description": description, "input_schema": input_schema(model)}
        for name, description, model, _ in TOOLS
    ]
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_router() -> ToolRouter:
    """Every security tool."""
    return ToolRouter(
        [
            ToolSpec(
                name=name,
                description=description,
                handler=handler,
                input_schema=input_schema(model),
                terminal=name == "ask_user",
            )
            for name, description, model, handler in TOOLS
        ]
    )


__all__ = [
    "INVALID_ARGUMENTS",
    "TOOLS",
    "TOOL_NAMES",
    "build_router",
    "event_line",
    "input_schema",
    "parse",
    "schema_document",
]
