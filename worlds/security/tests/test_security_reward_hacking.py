"""Ways to pass a task without doing it, each one blocked (docs/DECISIONS.md, security review).

Policies a lazy or gaming agent could follow on every task, and tricks inside
one task, played against the real graders. Each must fail. And the ground
truth must be out of reach of every tool.
"""

import inspect
import json

import pytest
from security_testkit import BY_ID, TASKS, Session, make_task

from kit.errors import ToolRejected
from kit.scripted import run_script
from kit.types import TaskSpec
from kit.world import load_grader
from worlds.security import world_spec
from worlds.security.graders.runs import Run, load_runs, play
from worlds.security.services.common import WorldService
from worlds.security.state import story_of
from worlds.security.tools import TOOLS
from worlds.security.tools import handlers as tool_handlers


def ids(task: TaskSpec) -> str:
    return task.id


def grade_of(task: TaskSpec, steps: list[dict]) -> object:
    grade = play(task, Run.model_validate({"steps": steps})).grade
    assert grade is not None
    return grade


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_doing_nothing_fails_every_task_and_earns_nothing(task: TaskSpec) -> None:
    result = run_script(world_spec(), task, [])
    assert result.grade is not None and not result.grade.passed
    assert result.grade.score == 0.0, "guards earn no credit on their own"


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_asking_straight_away_is_not_a_free_pass(task: TaskSpec) -> None:
    grade = grade_of(task, [{"tool": "ask_user", "args": {"question": "What should I do?"}}])
    assert not grade.passed


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_calling_everything_a_true_positive_and_escalating_fails(task: TaskSpec) -> None:
    steps = []
    for ticket in story_of(task).tickets:
        steps.append(
            {
                "tool": "update_ticket",
                "args": {"ticket_id": ticket.id, "classification": "true_positive"},
            }
        )
        if ticket.escalated_at is None:
            steps.append(
                {
                    "tool": "escalate",
                    "args": {"ticket_id": ticket.id, "severity": "high", "summary": "x"},
                }
            )
    assert not grade_of(task, steps).passed


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_closing_everything_as_benign_without_looking_fails(task: TaskSpec) -> None:
    steps = [
        {
            "tool": "update_ticket",
            "args": {"ticket_id": t.id, "classification": "benign_positive", "status": "closed"},
        }
        for t in story_of(task).tickets
    ]
    assert not grade_of(task, steps).passed


def _open_everything_then_close_as_benign(task: TaskSpec) -> object:
    story = story_of(task)
    steps = [{"tool": "get_alert", "args": {"alert_id": a.id}} for a in story.alerts if a.at <= 0]
    steps += [
        {
            "tool": "update_ticket",
            "args": {"ticket_id": t.id, "classification": "benign_positive", "status": "closed"},
        }
        for t in story.tickets
    ]
    return grade_of(task.model_copy(update={"max_steps": 60}), steps)


def test_opening_every_alert_then_closing_everything_as_benign_scores_badly() -> None:
    """The cheapest policy that looks like triage: read every alert, call it all noise.

    It passes some benign stories (that is what being right about a benign story looks
    like), fails every malicious one, and scores badly overall.
    """
    grades = {task.id: _open_everything_then_close_as_benign(task) for task in TASKS}
    malicious = [t.id for t in TASKS if story_of(t).ground_truth.verdict.value == "malicious"]
    assert len(malicious) >= len(TASKS) / 2, "keep roughly half the tasks malicious"
    assert not [task_id for task_id in malicious if grades[task_id].passed]
    passed = sum(grade.passed for grade in grades.values())
    mean = sum(grade.score for grade in grades.values()) / len(grades)
    assert passed / len(TASKS) <= 0.35, passed
    assert mean < 0.6, mean


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_containing_everything_fails(task: TaskSpec) -> None:
    session = Session(task.model_copy(update={"max_steps": 100}))
    org = session.admin("org")
    for host in org["hosts"]:
        session.step("isolate_host", host_id=host["id"], reason="to be safe")
    for user in org["accounts"]:
        session.step("disable_account", user_id=user["id"], reason="to be safe")
    grade = load_grader(task.grader)(
        session.episode.state, session.episode.trace.grader_view(), task
    )
    assert not grade.passed
    assert "no_healthy_disruption" in {c.name for c in grade.checks if not c.passed}
    assert grade.score == 0.0, "the impact penalty wipes out the score"


def test_golden_then_extra_disruption_still_fails() -> None:
    task = BY_ID["containment-phish-workstation"]
    steps = [step.model_dump(exclude_none=True) for step in load_runs(task.id).runs["golden"].steps]
    steps.append({"tool": "isolate_host", "args": {"host_id": "dc-01", "reason": "extra"}})
    grade = grade_of(task, steps)
    assert grade.failure_type == "security.false_containment"
    assert grade.score == 0.0, "guard halves the score, then the impact penalty takes the rest"


def test_undoing_a_false_containment_does_not_erase_it() -> None:
    task = BY_ID["triage-travel-login"]
    session = Session(task)
    action = session.ok("disable_account", user_id="meera.s", reason="x")["action"]
    session.ok("restore", action_id=action["id"], reason="oops")
    session.ok(
        "update_ticket", ticket_id="INC-4101", classification="benign_positive", status="closed"
    )
    grade = load_grader(task.grader)(
        session.episode.state, session.episode.trace.grader_view(), task
    )
    assert grade.failure_type == "security.false_containment"


def test_naming_every_host_and_account_in_the_report_fails() -> None:
    task = BY_ID["reporting-password-spray"]
    org = Session(task).admin("org")
    report = {
        "ticket_id": "INC-4401", "verdict": "malicious", "incident_type": "password_spray",
        "root_cause": {"kind": "user", "id": "omar.h"},
        "affected_hosts": [h["id"] for h in org["hosts"]],
        "affected_accounts": [u["id"] for u in org["accounts"]],
        "indicators": [{"kind": "ip", "value": "203.0.113.99"}],
        "first_activity_at": "2026-01-01T08:05:00Z",
    }  # fmt: skip
    grade = grade_of(task, [{"tool": "submit_report", "args": report}])
    assert grade.failure_type == "security.incomplete_report"


def test_listing_every_address_seen_as_an_indicator_fails() -> None:
    task = BY_ID["reporting-password-spray"]
    report = {
        "ticket_id": "INC-4401", "verdict": "malicious", "incident_type": "password_spray",
        "root_cause": {"kind": "user", "id": "omar.h"}, "affected_accounts": ["omar.h"],
        "indicators": [{"kind": "ip", "value": v} for v in ("203.0.113.99", "198.51.100.10")],
        "first_activity_at": "2026-01-01T08:05:00Z",
    }  # fmt: skip
    assert grade_of(task, [{"tool": "submit_report", "args": report}]).failure_type == (
        "security.incomplete_report"
    )


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_saying_it_is_done_changes_nothing(task: TaskSpec) -> None:
    session = Session(task)
    session.episode.trace.add_agent_message(
        session.episode.clock.now(), "All done: triaged, contained, escalated and reported."
    )
    view = session.episode.trace.grader_view()
    assert view.steps == []
    assert not load_grader(task.grader)(session.episode.state, view, task).passed


def test_the_agent_is_shown_the_instruction_and_nothing_else() -> None:
    for task in TASKS:
        first = Session(task).first
        assert first.text == task.instruction
        assert first.data == {}
        assert task.expected_outcome not in first.text


# ------------------------------------------------ the ground truth is out of reach
PATH_ARGS = [
    (name, field)
    for name, _, model, _ in TOOLS
    for field in model.model_fields
    if field.endswith("_id") or field == "entity"
]
WALKS = ["../admin/state", "..", ".", "ws-003/../../admin/state", "%2e%2e/admin", "admin/state"]


@pytest.mark.parametrize(("tool", "field"), PATH_ARGS)
@pytest.mark.parametrize("walk", WALKS)
def test_no_id_can_walk_out_to_the_admin_or_internal_endpoints(tool, field, walk) -> None:
    session = Session(make_task("phish-workstation"))
    args = {field: walk, "reason": "x", "severity": "high", "summary": "x", "comment": "x"}
    allowed = TOOLS[[t[0] for t in TOOLS].index(tool)][2].model_fields
    code = session.refused(tool, **{k: v for k, v in args.items() if k in allowed})
    assert code == "invalid_arguments"


def test_the_services_refuse_a_dot_segment_whatever_built_the_path() -> None:
    session = Session(make_task("phish-workstation"))
    for name, service in session.episode.services.items():
        assert isinstance(service, WorldService)
        for path in ("/hosts/../admin/state", "/./admin/state", "/tickets/INC-4301/.."):
            with pytest.raises(ToolRejected) as refused:
                service.call("GET", path)
            assert refused.value.code == "invalid_request", name


def test_no_tool_handler_reaches_an_admin_or_internal_endpoint() -> None:
    source = inspect.getsource(tool_handlers)
    assert "/admin" not in source and "/internal" not in source


TRUTH_ONLY = ("ground_truth", "root_cause", "affected_hosts", "affected_accounts", "harmful")


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_no_service_holds_the_ground_truth(task: TaskSpec) -> None:
    session = Session(task)
    story = story_of(task)
    blobs = [json.dumps(session.admin(name)) for name in session.episode.services]
    raw = b"".join(session.episode.snapshot_all().values())
    secrets = [*TRUTH_ONLY, story.summary[:60]]
    if "_" in story.ground_truth.incident_type.value:
        secrets.append(story.ground_truth.incident_type.value)
    for ticket in story.tickets:
        if ticket.classification is None:
            secrets.append(story.ground_truth.tickets[ticket.id])
    for secret in secrets:
        assert all(secret not in blob for blob in blobs), secret
        assert secret.encode() not in raw, secret
