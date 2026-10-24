"""Graders for the ``reporting`` tasks: the structured report matches the ground truth."""

from __future__ import annotations

from kit.trace import GraderTrace
from kit.types import GradeResult, TaskSpec
from kit.world import StateView

from .common import grade, no_healthy_disruption, no_over_containment, report_matches


def password_spray(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-password-spray: one account taken over, not the twelve that were tried."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-4401"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def mfa_fatigue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-mfa-fatigue: the CFO's account taken over after an MFA push flood."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-6101"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def web_shell(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-web-shell: malware on web-prod-01 through svc-web; db-prod-01 untouched."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-6201"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def mass_rename(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-mass-rename: ws-009 and kenji.t; file-01 was the victim, not compromised."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-6401"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def cloud_key(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-cloud-key: the leaked key's account, no host."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-6301"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def dns_tunnel(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-dns-tunnel: ws-012, both domains, no account."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-6501"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def phish_workstation(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-phish-workstation: ws-003 and priya.n, three indicators."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-4301"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def scanner_sweep(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """reporting-scanner-sweep: benign security testing, nothing affected."""
    return grade(
        state,
        trace,
        task,
        *report_matches("INC-5201"),
        no_healthy_disruption(),
        no_over_containment(),
    )
