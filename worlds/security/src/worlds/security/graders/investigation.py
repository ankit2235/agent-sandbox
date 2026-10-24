"""Graders for the ``investigation`` tasks: which alert is real, where it began, how far it went."""

from __future__ import annotations

from kit.trace import GraderTrace
from kit.types import GradeResult, TaskSpec
from kit.world import StateView

from .common import (
    Check,
    escalation_correct,
    grade,
    no_healthy_disruption,
    no_over_containment,
    report_indicators,
    report_root_cause,
    report_scope,
    report_verdict,
    tickets_classified,
)


def backup_or_exfil(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-backup-or-exfil: the backup is benign; the workstation upload is real."""
    return grade(
        state,
        trace,
        task,
        tickets_classified(),
        escalation_correct(),
        report_root_cause("INC-4202"),
        report_verdict("INC-4202"),
        report_scope("INC-4202"),
        report_indicators("INC-4202"),
        no_healthy_disruption(),
    )


def _investigated(ticket_id: str) -> tuple[Check, ...]:
    """Classified, escalated in time, and a report with the right start, scope and indicators."""
    return (
        tickets_classified(),
        escalation_correct(),
        report_root_cause(ticket_id),
        report_verdict(ticket_id),
        report_scope(ticket_id),
        report_indicators(ticket_id),
        no_healthy_disruption(),
    )


def web_shell(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-web-shell: web-prod-01 is patient zero; db-prod-01 was only probed."""
    return grade(state, trace, task, *_investigated("INC-6201"))


def cloud_key(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-cloud-key: the leaked svc-build key, no host involved."""
    return grade(state, trace, task, *_investigated("INC-6301"))


def dns_tunnel(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-dns-tunnel: ws-012, both domains, no account misused."""
    return grade(state, trace, task, *_investigated("INC-6501"))


def mass_rename(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-mass-rename: ws-009 is the source; file-01 is only the victim."""
    return grade(state, trace, task, *_investigated("INC-6401"))


def mfa_fatigue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-mfa-fatigue: the CFO's account only; no host is involved."""
    return grade(state, trace, task, *_investigated("INC-6101"))


def phish_scope(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-phish-scope: ws-003 and priya.n, three indicators, nothing else.

    The night shift already classified and escalated the ticket, so only the report counts.
    """
    return grade(
        state,
        trace,
        task,
        report_root_cause("INC-4301"),
        report_verdict("INC-4301"),
        report_scope("INC-4301"),
        report_indicators("INC-4301"),
        no_healthy_disruption(),
    )


def lateral_scope(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-lateral-scope: tomas.v on ws-010 and ws-011; the patching is benign."""
    return grade(state, trace, task, *_investigated("INC-4502"))


def _benign_report(ticket_id: str) -> tuple[Check, ...]:
    """A benign story written up as benign: no root cause, no scope, no indicators."""
    return (
        tickets_classified(),
        escalation_correct(),
        report_verdict(ticket_id),
        report_root_cause(ticket_id),
        report_scope(ticket_id),
        report_indicators(ticket_id),
        no_healthy_disruption(),
        no_over_containment(),
    )


def unsigned_update(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-unsigned-update: the approved rollout, written up as benign software."""
    return grade(state, trace, task, *_benign_report("INC-5401"))


def privileged_group_change(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """investigation-privileged-group-change: an approved change, written up as admin activity."""
    return grade(state, trace, task, *_benign_report("INC-5801"))
