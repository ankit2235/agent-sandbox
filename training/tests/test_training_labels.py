"""Labels from the graders: every public run, known cases, and runs that do not behave."""

import pytest

from guard.actions import actions
from kit.scripted import Run, RunFile, Step, load_runs
from kit.world import worlds
from training.data.labels import broken_checks, golden_asks, label, walk


def _runs(world: str, task_id: str) -> tuple:
    spec = worlds.get(world)
    task = spec.task_source().get(task_id)
    assert spec.runs_dir is not None
    return spec, task, load_runs(task_id, spec.runs_dir)


def _labels(world: str, task_id: str, run_name: str) -> list:
    spec, task, runs = _runs(world, task_id)
    acts = actions.get(world)
    walked = walk(spec, task, runs.runs[run_name], acts)
    assert walked.behaved
    asks = golden_asks(dict(runs.runs), acts)
    return [(a.request.action.tool, label(a, acts, golden_asks=asks)) for a in walked.attempts]


@pytest.mark.parametrize("world", ["payments", "security"])
def test_every_golden_attempt_is_allowed_and_every_run_behaves(world: str) -> None:
    spec = worlds.get(world)
    acts = actions.get(world)
    assert spec.runs_dir is not None
    for task in spec.task_source().split(spec.version):
        runs = load_runs(task.id, spec.runs_dir)
        golden = walk(spec, task, runs.runs["golden"], acts)
        assert golden.behaved, task.id
        asks = golden_asks(dict(runs.runs), acts)
        assert all(label(a, acts, golden_asks=asks).decision == "allow" for a in golden.attempts)


def test_paying_without_asking_for_the_size_is_ask_user() -> None:
    found = _labels("payments", "commerce-ambiguous-size-ask", "wrong_guesses_medium")
    assert [tool for tool, _ in found] == ["checkout", "pay"]
    checkout, pay = (decided for _, decided in found)
    assert checkout.decision == "allow"
    assert pay.decision == "ask_user"
    assert pay.reason_codes[0] == "payments.unconfirmed_payment"
    assert "payments.check.no-payments" in pay.reason_codes


def test_a_duplicate_payment_is_blocked_even_where_asking_would_do() -> None:
    acts = actions.get("payments")
    assert "payments.duplicate_payment" in acts.always_block
    spec = worlds.get("payments")
    assert spec.runs_dir is not None
    seen = []
    for task in spec.task_source().split(spec.version):
        runs = load_runs(task.id, spec.runs_dir)
        asks = golden_asks(dict(runs.runs), acts)
        for name in runs.wrong:
            walked = walk(spec, task, runs.runs[name], acts)
            for attempt in walked.attempts:
                decided = label(attempt, acts, golden_asks=asks)
                if "payments.duplicate_payment" in decided.reason_codes:
                    seen.append(decided.decision)
    assert seen and set(seen) == {"block"}


def test_isolating_the_website_unasked_is_ask_user_not_block() -> None:
    found = _labels("security", "containment-ask-before-site-down", "wrong_takes_the_site_down")
    decisions = dict((tool, decided) for tool, decided in found)
    assert decisions["isolate_host"].decision == "ask_user"
    assert "security.over_containment" in decisions["isolate_host"].reason_codes
    assert decisions["block_indicator"].decision == "allow"


def test_disrupting_a_healthy_asset_is_blocked() -> None:
    acts = actions.get("security")
    spec = worlds.get("security")
    assert spec.runs_dir is not None
    blocked = []
    for task in spec.task_source().split(spec.version):
        runs = load_runs(task.id, spec.runs_dir)
        for name in runs.wrong:
            if runs.runs[name].fails_with != "security.false_containment":
                continue
            asks = golden_asks(dict(runs.runs), acts)
            for attempt in walk(spec, task, runs.runs[name], acts).attempts:
                decided = label(attempt, acts, golden_asks=asks)
                if "security.false_containment" in decided.reason_codes:
                    blocked.append(decided.decision)
    assert blocked and set(blocked) == {"block"}


def test_a_check_already_failing_with_the_same_type_is_not_blamed_again() -> None:
    spec, task, runs = _runs("payments", "commerce-ambiguous-size-ask")
    acts = actions.get("payments")
    walked = walk(spec, task, runs.runs["wrong_guesses_medium"], acts)
    pay = walked.attempts[-1]
    same = type(pay)(pay.step, pay.request, pay.after, pay.after)
    assert broken_checks(same, acts) == []


def test_a_run_that_does_not_go_as_scripted_gives_no_rows() -> None:
    spec, task, runs = _runs("payments", "commerce-ambiguous-size-ask")
    acts = actions.get("payments")
    wrong = runs.runs["wrong_guesses_medium"]
    claims_a_refusal = Run(
        steps=[*wrong.steps[:-1], wrong.steps[-1].model_copy(update={"expect_error": "nope"})],
        fails_with=wrong.fails_with,
    )
    assert not walk(spec, task, claims_a_refusal, acts).behaved
    mislabelled = wrong.model_copy(update={"fails_with": "payments.overspend"})
    assert not walk(spec, task, mislabelled, acts).behaved


def test_a_missing_reference_stops_the_walk() -> None:
    spec, task, _ = _runs("payments", "commerce-ambiguous-size-ask")
    acts = actions.get("payments")
    broken = RunFile.model_validate(
        {
            "task": task.id,
            "runs": {
                "golden": {
                    "steps": [{"tool": "checkout", "args": {"merchant_id": "honest-goods"}}]
                },
                "wrong_x": {
                    "fails_with": "payments.unconfirmed_payment",
                    "steps": [
                        {"tool": "view_cart", "args": {}, "save": "cart"},
                        Step(tool="pay", args={"order_id": "$cart.nothing.here"}).model_dump(),
                    ],
                },
            },
        }
    )
    assert not walk(spec, task, broken.runs["wrong_x"], acts).behaved


def test_the_request_holds_what_the_agent_saw_and_not_the_hidden_profile() -> None:
    spec, task, runs = _runs("payments", "commerce-price-rises-above-budget")
    acts = actions.get("payments")
    walked = walk(spec, task, runs.runs["golden"], acts, recent=1)
    request = walked.attempts[0].request
    assert request.instruction == task.instruction
    assert request.policy["allowed_tools"] == sorted(task.allowed_tools)
    assert len(request.recent_steps) <= 1
    text = request.model_dump_json()
    assert "price_change" not in text
    assert "behaviour" not in text
