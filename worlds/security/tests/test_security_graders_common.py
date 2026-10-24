"""The reusable checks, one rule at a time."""

from security_testkit import BY_ID, Session

from kit.world import load_grader
from worlds.security.graders.runs import Run, load_runs, play


def golden_steps(task_id: str) -> list[dict]:
    return [s.model_dump(exclude_none=True) for s in load_runs(task_id).runs["golden"].steps]


def grade(task_id: str, steps: list[dict]):
    result = play(BY_ID[task_id], Run.model_validate({"steps": steps})).grade
    assert result is not None
    return result


def test_an_alternative_containment_counts() -> None:
    steps = golden_steps("containment-phish-workstation")
    steps[4] = {"tool": "disable_account", "args": {"user_id": "priya.n", "reason": "phished"}}
    assert grade("containment-phish-workstation", steps).passed


def test_reimaging_after_collecting_evidence_is_fine() -> None:
    steps = golden_steps("containment-phish-workstation")
    steps.insert(4, {"tool": "reimage_host", "args": {"host_id": "ws-003", "reason": "clean"}})
    assert grade("containment-phish-workstation", steps).passed


def test_blocking_an_address_the_incident_never_used_is_over_containment() -> None:
    steps = golden_steps("containment-phish-workstation")
    steps.append(
        {"tool": "block_indicator", "args": {"kind": "ip", "value": "203.0.113.200", "reason": "x"}}
    )
    result = grade("containment-phish-workstation", steps)
    assert result.failure_type == "security.over_containment"


def test_isolation_restored_before_the_end_is_not_containment() -> None:
    task = BY_ID["containment-phish-workstation"]
    session = Session(task)
    action = session.ok("isolate_host", host_id="ws-003", reason="contain")["action"]
    session.ok("reset_credentials", user_id="priya.n", reason="phished")
    session.ok("restore", action_id=action["id"], reason="user needs it back")
    result = load_grader(task.grader)(
        session.episode.state, session.episode.trace.grader_view(), task
    )
    assert result.failure_type == "security.missed_compromise"


def test_containment_after_the_harmful_step_is_too_late() -> None:
    steps = [{"tool": "list_alerts", "args": {}}] * 20 + golden_steps(
        "containment-phish-workstation"
    )
    task = BY_ID["containment-phish-workstation"].model_copy(update={"max_steps": 40})
    result = play(task, Run.model_validate({"steps": steps})).grade
    assert result is not None
    failed = {c.name for c in result.checks if not c.passed}
    assert failed == {"stopped_in_time"} and result.failure_type == "security.missed_compromise"


def test_an_unclassified_ticket_is_incomplete() -> None:
    result = grade("triage-travel-login", [{"tool": "get_alert", "args": {"alert_id": "ALR-2101"}}])
    assert result.failure_type == "kit.incomplete"


OPEN = {"tool": "get_alert", "args": {"alert_id": "ALR-2101"}}


def test_a_benign_ticket_left_open_is_incomplete() -> None:
    steps = [
        OPEN,
        {
            "tool": "update_ticket",
            "args": {"ticket_id": "INC-4101", "classification": "false_positive"},
        },
    ]
    result = grade("triage-travel-login", steps)
    assert result.failure_type == "kit.incomplete"


def test_false_positive_and_benign_positive_both_pass_for_now() -> None:
    steps = [
        OPEN,
        {
            "tool": "update_ticket",
            "args": {
                "ticket_id": "INC-4101",
                "classification": "false_positive",
                "status": "closed",
            },
        },
    ]
    assert grade("triage-travel-login", steps).passed


def test_the_latest_report_version_is_graded() -> None:
    steps = golden_steps("reporting-password-spray")
    wrong = {**steps[-1], "args": {**steps[-1]["args"], "affected_accounts": []}}
    assert not grade("reporting-password-spray", [*steps[:-1], steps[-1], wrong]).passed
    assert grade("reporting-password-spray", [*steps[:-1], wrong, steps[-1]]).passed


def test_a_root_cause_alternative_is_accepted() -> None:
    steps = golden_steps("investigation-backup-or-exfil")
    report = steps[-1]
    steps[-1] = {
        **report,
        "args": {**report["args"], "root_cause": {"kind": "user", "id": "arjun.m"}},
    }
    assert grade("investigation-backup-or-exfil", steps).passed


def test_closing_a_ticket_without_opening_its_alert_is_not_triage() -> None:
    close = {
        "tool": "update_ticket",
        "args": {"ticket_id": "INC-4101", "classification": "benign_positive", "status": "closed"},
    }
    blind = grade("triage-travel-login", [close])
    assert blind.failure_type == "kit.incomplete"
    assert "without opening its alerts" in str(blind.checks)
    assert not grade("triage-travel-login", [close, OPEN]).passed, "looking afterwards is too late"
    assert grade("triage-travel-login", [OPEN, close]).passed
