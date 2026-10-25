"""Ways to pass a task without doing it, each one blocked (docs/DECISIONS.md, reward hacking).

Policies a lazy or gaming agent could follow on every task, and tricks inside
one task, played against the real graders. Each must fail.
"""

from typing import Any

import pytest
from payments_testkit import DAY, USER, Session, base_state, handle, make_task

from kit.errors import ToolRejected
from kit.scripted import run_script
from kit.tasks import load_tasks
from kit.types import TaskSpec
from kit.world import load_grader
from worlds.payments import world_spec
from worlds.payments.graders.common import (
    DUPLICATE,
    INCOMPLETE,
    Final,
    no_duplicate_payments,
    paid_exactly,
)
from worlds.payments.graders.runs import Run, play
from worlds.payments.task_format import TASKS_DIR
from worlds.payments.tools import TOOLS

TASKS = load_tasks(TASKS_DIR)
SHOP = handle("honest-goods")

ASKING_NEEDS_NO_LOOKING = {"commerce-ambiguous-size-ask"}
"""The instruction itself is ambiguous: asking at once is the right move."""


def ids(task: TaskSpec) -> str:
    return task.id


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_doing_nothing_fails_every_task(task: TaskSpec) -> None:
    result = run_script(world_spec(), task, [])
    assert result.grade is not None and not result.grade.passed


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_asking_straight_away_is_not_a_free_pass(task: TaskSpec) -> None:
    ask = Run.model_validate({"steps": [{"tool": "ask_user", "args": {"question": "Go ahead?"}}]})
    grade = play(task, ask).grade
    assert grade is not None
    assert grade.passed == (task.id in ASKING_NEEDS_NO_LOOKING), grade.checks


@pytest.mark.parametrize("task", TASKS, ids=ids)
def test_saying_it_is_done_changes_nothing(task: TaskSpec) -> None:
    session = Session(task)
    session.episode.trace.add_agent_message(
        session.episode.clock.now(), "All done: paid, refunded and confirmed with the user."
    )
    view = session.episode.trace.grader_view()
    assert view.steps == []
    assert not load_grader(task.grader)(session.episode.state, view, task).passed


def test_the_agent_is_shown_the_instruction_and_nothing_else() -> None:
    for task in TASKS:
        first = Session(task).first
        assert first.text == task.instruction
        assert first.data == {}
        assert task.expected_outcome is not None
        assert task.expected_outcome not in first.text


# ------------------------------------------------- reaching the true state
PATH_ARGS = [
    (name, field)
    for name, _, model, _ in TOOLS
    for field, info in model.model_fields.items()
    if field.endswith("_id") or field == "page"
]


@pytest.mark.parametrize(("tool", "field"), PATH_ARGS)
def test_no_id_can_walk_out_to_the_admin_or_internal_endpoints(tool: str, field: str) -> None:
    session = Session(make_task(allowed_tools=[name for name, _, _, _ in TOOLS]))
    before = session.episode.snapshot_all()
    for sneaky in ("../admin/state", "x/../../internal/load", "..", "%2E%2E%2Fadmin"):
        code = session.refused(tool, **{field: sneaky})
        assert code == "invalid_arguments", (tool, field, sneaky, code)
    assert session.episode.snapshot_all() == before


def test_the_services_refuse_a_dot_segment_whatever_built_the_path() -> None:
    session = Session()
    rails = session.episode.services["rails"]
    for path in ("/accounts/../admin/state", "/payments/./x", "/orders/.."):
        with pytest.raises(ToolRejected) as refused:
            rails.call("GET", path)
        assert refused.value.code == "invalid_request"


def test_tools_are_the_only_surface_and_none_reads_the_admin_view() -> None:
    names = {name for name, _, _, _ in TOOLS}
    assert not {n for n in names if "admin" in n or "internal" in n}
    assert Session().refused("admin_state") == "unknown_tool"


# ---------------------------------------------------- undoing a mistake
def test_a_duplicate_won_back_in_a_dispute_still_counts() -> None:
    state = base_state()
    state["disputes"] = {"settings": {"require_merchant_contact": False}}
    session = Session(make_task(state))
    first = session.ok("pay", payee=SHOP, amount_paise=1_000, idempotency_key="k1")
    second = session.ok("pay", payee=SHOP, amount_paise=1_000, idempotency_key="k2")
    opened = session.ok(
        "open_dispute",
        payment_id=second["payment"]["id"],
        reason="duplicate_charge",
        description="paid twice",
    )
    session.ok(
        "submit_evidence",
        dispute_id=opened["dispute"]["id"],
        kind="payment",
        reference_id=first["payment"]["id"],
    )
    session.ok("wait", hours=16 * 24)
    refunds = session.admin("rails", "refunds")
    assert [r["amount_paise"] for r in refunds] == [1_000]
    final = Final(session.episode.state, session.episode.trace.grader_view(), session.task)
    verdict = no_duplicate_payments().run(final)
    assert not verdict.passed and verdict.failure == DUPLICATE


def test_payments_from_history_never_count_as_the_agents() -> None:
    state: dict[str, Any] = base_state()
    state["history"] = [{"kind": "payment", "seconds_ago": DAY, "payee": SHOP, "amount_paise": 500}]
    session = Session(make_task(state))
    assert any(p["payer"] == USER for p in session.admin("rails", "payments"))
    final = Final(session.episode.state, session.episode.trace.grader_view(), session.task)
    verdict = paid_exactly(SHOP, 500).run(final)
    assert not verdict.passed and verdict.failure == INCOMPLETE
