"""The run-file player: refs, expectations, and run files that cannot be trusted."""

from typing import Any

import pytest
from payments_testkit import make_task
from pydantic import ValidationError

from kit.scripted import ScriptRun
from worlds.payments.graders.runs import Run, RunError, RunFile, play

HONEST = "honest-goods"
TASK = make_task(task_id="runs-player", allowed_tools=[]).model_copy(
    update={"grader": "worlds.payments.graders.commerce:price_rises_above_budget"}
)


def run(*steps: dict[str, Any]) -> Run:
    return Run.model_validate({"steps": list(steps)})


ADD = {
    "tool": "add_to_cart",
    "args": {"merchant_id": HONEST, "product_id": f"{HONEST}-kettle", "quantity": 1},
}
CHECKOUT = {"tool": "checkout", "args": {"merchant_id": HONEST}, "save": "co"}


def test_refs_are_filled_from_saved_results_with_their_types() -> None:
    result = play(
        TASK,
        run(
            ADD,
            CHECKOUT,
            {
                "tool": "pay",
                "args": {
                    "payee": "$co.payment_request.payee",
                    "amount_paise": "$co.payment_request.amount_paise",
                    "order_id": "$co.order.id",
                    "idempotency_key": "k1",
                },
                "expect": {"outcome": "paid", "payment": {"amount_paise": 149_900}},
            },
        ),
    )
    assert isinstance(result, ScriptRun)
    assert result.grade is not None
    pay = result.trace.tool_calls("pay")[0]
    assert pay.args == {
        "payee": f"{HONEST}@sandbox",
        "amount_paise": 149_900,
        "order_id": result.trace.tool_calls("checkout")[0].response["order"]["id"],
        "idempotency_key": "k1",
    }


def test_an_unexpected_refusal_fails_the_run() -> None:
    with pytest.raises(RunError, match="step 1 \\(checkout\\) was refused: cart_empty"):
        play(TASK, run(CHECKOUT))


def test_an_expected_refusal_is_fine_and_a_missing_one_is_not() -> None:
    play(TASK, run({**CHECKOUT, "expect_error": "cart_empty"}))
    with pytest.raises(RunError, match="expected refusal cart_empty, got accepted"):
        play(TASK, run(ADD, {**CHECKOUT, "expect_error": "cart_empty"}))


def test_an_expectation_that_does_not_hold_fails_the_run() -> None:
    wrong = {**CHECKOUT, "expect": {"payment_request": {"amount_paise": 1}}}
    with pytest.raises(RunError, match="step 2 \\(checkout\\): expected"):
        play(TASK, run(ADD, wrong))


def test_a_ref_to_a_refused_step_or_a_missing_field_fails_the_run() -> None:
    refused = {**CHECKOUT, "expect_error": "cart_empty"}
    use = {"tool": "get_order", "args": {"order_id": "$co.order.id"}}
    with pytest.raises(RunError, match="was refused"):
        play(TASK, run(refused, use))
    missing = {"tool": "get_order", "args": {"order_id": "$co.nothing.id"}}
    with pytest.raises(RunError, match="'nothing' is not in the result"):
        play(TASK, run(ADD, CHECKOUT, missing))


def test_a_run_that_ends_before_its_last_step_fails() -> None:
    ask = {"tool": "ask_user", "args": {"question": "?"}}
    with pytest.raises(RunError, match="ended after step 1 of 2"):
        play(TASK, run(ask, ADD))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"steps": [{"tool": "get_order", "args": {"order_id": "$co.id"}}]}, "no earlier step"),
        ({"steps": [CHECKOUT, CHECKOUT]}, "a second time"),
        ({"steps": []}, "at least 1"),
    ],
)
def test_a_run_must_be_well_formed(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Run.model_validate(raw)


@pytest.mark.parametrize(
    ("runs", "message"),
    [
        ({"wrong_x": {"steps": [ADD], "fails_with": "kit.other"}}, "needs a 'golden' run"),
        ({"golden": {"steps": [ADD]}}, "at least one wrong"),
        ({"golden": {"steps": [ADD]}, "wrong_x": {"steps": [ADD]}}, "give fails_with"),
        (
            {"golden": {"steps": [ADD]}, "bad": {"steps": [ADD], "fails_with": "kit.other"}},
            "named wrong_",
        ),
    ],
)
def test_a_run_file_needs_a_golden_and_a_wrong_run(runs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        RunFile.model_validate({"task": "some-task", "runs": runs})
