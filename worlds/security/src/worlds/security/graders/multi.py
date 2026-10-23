"""Graders for the ``multi`` tasks: triage, escalation, containment and report together."""

from __future__ import annotations

from kit.trace import GraderTrace
from kit.types import GradeResult, TaskSpec
from kit.world import StateView

from .common import (
    Check,
    contained,
    escalation_correct,
    evidence_preserved,
    grade,
    no_healthy_disruption,
    no_over_containment,
    report_indicators,
    report_matches,
    report_root_cause,
    report_scope,
    report_verdict,
    stopped_in_time,
    tickets_classified,
)


def maintenance_vs_lateral(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-maintenance-vs-lateral: patching is benign; tomas.v is the incident, critical SLA."""
    return grade(
        state,
        trace,
        task,
        tickets_classified(),
        escalation_correct(),
        evidence_preserved(),
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
        *report_matches("INC-4502"),
    )


def _handled(ticket_id: str) -> tuple[Check, ...]:
    """Everything: triage, escalation, containment in time, evidence, the report."""
    return (
        tickets_classified(),
        escalation_correct(),
        evidence_preserved(),
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
        *report_matches(ticket_id),
    )


def morning_queue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-morning-queue: four tickets, one real takeover to escalate, reset and report."""
    return grade(state, trace, task, *_handled("INC-7004"))


def phish_simulation_vs_real(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-phish-simulation-vs-real: leave the vendor alone, stop li.w's takeover."""
    return grade(state, trace, task, *_handled("INC-7102"))


def mass_rename(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-mass-rename: critical SLA, isolate ws-009 before the HR share, report."""
    return grade(state, trace, task, *_handled("INC-6401"))


def mfa_fatigue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-mfa-fatigue: the CFO's account, from triage to report."""
    return grade(state, trace, task, *_handled("INC-6101"))


def dns_tunnel(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-dns-tunnel: ws-012 from triage to report."""
    return grade(state, trace, task, *_handled("INC-6501"))


def cloud_key(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-cloud-key: the leaked key, reset before the next export, reported."""
    return grade(state, trace, task, *_handled("INC-6301"))


def exfil(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-exfil: the backup is benign; ws-004 is contained and reported."""
    return grade(state, trace, task, *_handled("INC-4202"))


def _benign(ticket_id: str) -> tuple[Check, ...]:
    """A benign story handled end to end: closed, nothing touched, written up as benign."""
    return (
        tickets_classified(),
        escalation_correct(),
        no_healthy_disruption(),
        no_over_containment(),
        report_verdict(ticket_id),
        report_root_cause(ticket_id),
        report_scope(ticket_id),
        report_indicators(ticket_id),
    )


def restore_test(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-restore-test: the approved restore test, handled and written up as benign."""
    return grade(state, trace, task, *_benign("INC-5501"))


def password_reset_burst(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """multi-password-reset-burst: helpdesk resets on request, handled as benign."""
    return grade(state, trace, task, *_benign("INC-5601"))
