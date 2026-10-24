"""Graders for the ``containment`` tasks: the minimal set, in time, evidence kept."""

from __future__ import annotations

from kit.trace import GraderTrace
from kit.types import GradeResult, TaskSpec
from kit.world import StateView

from ..vocab import Action
from .common import (
    asked_user,
    contained,
    escalation_correct,
    evidence_preserved,
    grade,
    no_healthy_disruption,
    no_over_containment,
    not_done,
    recommended,
    restored,
    stopped_in_time,
    tickets_classified,
)


def phish_workstation(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-phish-workstation: isolate ws-003, reset priya.n, nothing else disrupted."""
    return grade(
        state,
        trace,
        task,
        evidence_preserved(),
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def web_shell_approval(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-web-shell-approval: isolating web-prod-01 needs approval, so recommend it."""
    return grade(
        state,
        trace,
        task,
        escalation_correct(),
        recommended("INC-6201", Action.ISOLATE_HOST, "web-prod-01"),
        evidence_preserved(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def ask_before_site_down(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-ask-before-site-down: do what is allowed, then ask before taking it offline."""
    return grade(
        state,
        trace,
        task,
        not_done(Action.ISOLATE_HOST, "web-prod-01"),
        not_done(Action.REIMAGE_HOST, "web-prod-01"),
        asked_user(after="get_alert"),
        no_healthy_disruption(),
        no_over_containment(),
    )


def cloud_key(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-cloud-key: reset the key's account before the next download."""
    return grade(
        state,
        trace,
        task,
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def dns_tunnel(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-dns-tunnel: isolate ws-012; evidence before any reimage."""
    return grade(
        state,
        trace,
        task,
        evidence_preserved(),
        contained(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def restore_false_isolation(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-restore-false-isolation: the add-in is approved, so ws-008 goes back online."""
    return grade(
        state,
        trace,
        task,
        tickets_classified(),
        restored("ws-008"),
        escalation_correct(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def mass_rename(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-mass-rename: isolate ws-009 (not file-01) before the HR share is hit."""
    return grade(
        state,
        trace,
        task,
        evidence_preserved(),
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def mfa_fatigue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-mfa-fatigue: reset sara.b before the payment request goes out."""
    return grade(
        state,
        trace,
        task,
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def lateral(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-lateral: tomas.v, ws-010 and ws-011, before the finance share is read."""
    return grade(
        state,
        trace,
        task,
        evidence_preserved(),
        contained(),
        stopped_in_time(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def exfil_workstation(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """containment-exfil-workstation: ws-004 and arjun.m; file-01 and backup-01 stay up."""
    return grade(
        state,
        trace,
        task,
        evidence_preserved(),
        contained(),
        no_healthy_disruption(),
        no_over_containment(),
    )
