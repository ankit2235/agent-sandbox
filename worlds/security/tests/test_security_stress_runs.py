"""Security's modifiers through ``stress.core``: runs for every task, the alias, the guards."""

import pytest
from security_testkit import BY_ID, TASKS

from kit.scripted import Run, load_runs, play
from kit.types import TaskSpec
from stress.core import NotApplicable, apply, base_id, registry, scripted_runs
from worlds.security import world_spec
from worlds.security.graders.runs import RUNS_DIR
from worlds.security.state import story_of
from worlds.security.stress import (
    DECOY_TICKET,
    FLOOD_COUNT,
    MODIFIERS,
    STRESS,
    TIME_LEFT_MINUTES,
    noise,
)

SPEC = world_spec()


def runs_for(task: TaskSpec):
    return scripted_runs(task, load_runs(base_id(task), RUNS_DIR))


def guard(task: TaskSpec, steps: list, name: str):
    result = play(SPEC, task, Run.model_validate({"steps": steps}))
    return next(c for c in result.grade.checks if c.name == name), result.grade


@pytest.mark.parametrize("name", sorted(MODIFIERS))
@pytest.mark.parametrize("task_id", sorted(BY_ID))
def test_a_stressed_task_is_solvable_and_its_trap_fails(name: str, task_id: str) -> None:
    try:
        task = apply(BY_ID[task_id], [name], 7)
    except NotApplicable:
        return
    try:
        runs = runs_for(task)
    except NotApplicable as reason:
        assert name == "time_pressure" and "makes no escalation" in str(reason), reason
        return
    golden = play(SPEC, task, runs.runs["golden"])
    assert golden.passed, [c.detail for c in golden.grade.failed_checks]
    for wrong in runs.wrong:
        result = play(SPEC, task, runs.runs[wrong])
        assert result.grade.failure_type == runs.runs[wrong].fails_with


def test_the_world_registers_its_modifiers_and_the_generic_alias() -> None:
    assert registry.get("security") is STRESS
    assert STRESS.names() == sorted(MODIFIERS)
    task = BY_ID["multi-maintenance-vs-lateral"]
    assert apply(task, ["conflicting_instructions"], 3) == apply(task, ["misleading_note"], 3)
    assert STRESS.modifiers["alert_flood"].extra_steps == 2 * FLOOD_COUNT + 2


def test_core_apply_matches_the_worlds_own_apply_but_allows_the_extra_steps() -> None:
    from worlds.security.stress import apply as world_apply

    task = BY_ID["triage-mfa-fatigue"]
    ours, theirs = apply(task, ["time_pressure"], 5), world_apply(task, ["time_pressure"], 5)
    assert ours.model_copy(update={"max_steps": theirs.max_steps}) == theirs
    assert ours.max_steps == theirs.max_steps + TIME_LEFT_MINUTES + 1


def test_noise_finds_what_the_flood_and_the_decoy_added() -> None:
    task = apply(BY_ID["containment-phish-workstation"], ["alert_flood", "decoy_anomaly"], 2)
    tickets, entities = noise(story_of(task))
    assert len(tickets) == FLOOD_COUNT + 1 and DECOY_TICKET in tickets
    assert not entities & set(story_of(task).ground_truth.affected_hosts)
    assert noise(story_of(BY_ID["containment-phish-workstation"])) == (set(), set())


def test_treating_flood_noise_as_real_fails_even_where_the_grader_names_its_tickets() -> None:
    task = apply(BY_ID["containment-phish-workstation"], ["alert_flood"], 2)
    runs = runs_for(task)
    check, grade = guard(task, runs.runs["wrong_alert_flood"].steps, "left_stress_noise_alone")
    assert not check.passed and grade.failure_type == "security.false_positive_escalated"
    check, _ = guard(task, runs.runs["golden"].steps, "left_stress_noise_alone")
    assert check.passed


def test_missing_logs_must_not_change_a_verdict() -> None:
    task = apply(BY_ID["containment-phish-workstation"], ["partial_telemetry"], 1)
    runs = runs_for(task)
    check, grade = guard(task, runs.runs["wrong_partial_telemetry"].steps, "verdicts_held")
    assert not check.passed and grade.failure_type == "security.missed_compromise"


def test_a_late_escalation_fails_under_pressure_even_where_escalation_is_not_graded() -> None:
    task = apply(BY_ID["containment-web-shell-approval"], ["time_pressure"], 0)
    runs = runs_for(task)
    first = [s.tool for s in runs.runs["golden"].steps][:3]
    assert "escalate" in first, "with five minutes left, the golden run escalates first"
    check, grade = guard(task, runs.runs["wrong_time_pressure"].steps, "escalated_in_time")
    assert not check.passed and grade.failure_type == "security.late_escalation"


def test_every_modifier_has_runs_for_most_tasks() -> None:
    counts = {name: 0 for name in MODIFIERS}
    for task in TASKS:
        for name in MODIFIERS:
            try:
                runs_for(apply(task, [name], 7))
            except NotApplicable:
                continue
            counts[name] += 1
    assert counts["alert_flood"] == counts["decoy_anomaly"] == len(TASKS)
    assert counts["partial_telemetry"] == len(TASKS)
    assert counts["misleading_note"] >= 30 and counts["time_pressure"] >= 15
