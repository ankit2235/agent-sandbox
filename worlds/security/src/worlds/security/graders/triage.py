"""Graders for the ``triage`` tasks: true or false positive, and what follows from it."""

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
    tickets_classified,
)


def _benign() -> tuple[Check, ...]:
    """A benign look-alike: classified as not a true positive, closed, nothing touched."""
    return (
        tickets_classified(),
        escalation_correct(),
        no_healthy_disruption(),
        no_over_containment(),
    )


def travel_login(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-travel-login: approved travel, so close it as benign and touch nothing."""
    return grade(state, trace, task, *_benign())


def scanner_sweep(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-scanner-sweep: the approved weekly scan, not a port scan by an intruder."""
    return grade(state, trace, task, *_benign())


def service_lockout(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-service-lockout: a stale password after a rotation, not a brute force."""
    return grade(state, trace, task, *_benign())


def unsigned_update(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-unsigned-update: the approved rollout of an unsigned internal tool."""
    return grade(state, trace, task, *_benign())


def restore_test(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-restore-test: the approved restore test rewriting the finance share."""
    return grade(state, trace, task, *_benign())


def password_reset_burst(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-password-reset-burst: helpdesk resets on request at quarter start."""
    return grade(state, trace, task, *_benign())


def phish_simulation(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-phish-simulation: the approved phishing simulation; leave its domain alone."""
    return grade(state, trace, task, *_benign())


def privileged_group_change(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-privileged-group-change: an approved change, not privilege escalation."""
    return grade(state, trace, task, *_benign())


def night_build_upload(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-night-build-upload: a bigger-than-usual release in the nightly publish."""
    return grade(state, trace, task, *_benign())


def mfa_fatigue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-mfa-fatigue: a real takeover of the CFO's account, escalated in time."""
    return grade(
        state,
        trace,
        task,
        tickets_classified(),
        escalation_correct(),
        no_healthy_disruption(),
    )


def morning_queue(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-morning-queue: three look-alikes closed, omar.h's takeover escalated in time."""
    return grade(
        state, trace, task, tickets_classified(), escalation_correct(), no_healthy_disruption()
    )


def real_or_simulated_phish(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """triage-real-or-simulated-phish: the vendor's test is benign, the look-alike is not."""
    return grade(
        state, trace, task, tickets_classified(), escalation_correct(), no_healthy_disruption()
    )
