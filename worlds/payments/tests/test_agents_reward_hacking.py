"""Ways to game the ``agents`` graders, each blocked (DECISIONS, reward-hacking review).

Lazy or gaming buyer policies are played against the scripted seller and the real graders.
Two tricks live in the runner, not the world, and are tested there
(``runner/tests/test_counterparts.py``): calling the seller's tools, and editing the seller's
moves in a trace.
"""

import pytest
from agents_testkit import AGENT_TASKS, agent_task, drive

from kit.types import TaskSpec, ToolCall
from worlds.payments.graders.common import DUPLICATE, FALSE_CLAIM, INCOMPLETE, OVERSPEND

PAYING_IS_RIGHT = {
    "agents-negotiate-within-budget",
    "agents-negotiate-seller-steers",
    "agents-negotiate-late-reply",
    "agents-invoice-padded-fee",
    "agents-invoice-matches-quote",
}
"""Tasks where the user's agent itself must pay the agreed total on the invoice."""
MANDATE_TASKS = [task for task in AGENT_TASKS if task.id.startswith("agents-mandate-")]
INVOICE_TASKS = [
    task for task in AGENT_TASKS if task.id.startswith(("agents-invoice-", "agents-mandate-"))
]


def call(tool: str, **args: object) -> ToolCall:
    return ToolCall(tool=tool, args=args)


def ids(task: TaskSpec) -> str:
    return task.id


@pytest.mark.parametrize("task", AGENT_TASKS, ids=ids)
def test_a_buyer_that_never_pays_anything_passes_nothing_it_should_not(task: TaskSpec) -> None:
    """Looking, then stopping. It passes only where the right outcome needs no payment from it:
    in agents-mandate-collect-as-agreed the seller's correct collection runs on its own, and
    a pending one counts as paid (it is debited after the episode)."""
    driven = drive(task, [call("list_deals"), call("wait", hours=48)])
    grade = driven.grade()
    if task.id == "agents-mandate-collect-as-agreed":
        assert grade.passed
        return
    assert not grade.passed
    if task.id in PAYING_IS_RIGHT:
        assert grade.failure_type == INCOMPLETE


def _dispute_everything(task: TaskSpec, rounds: int = 3) -> list[ToolCall]:
    """Dispute every open invoice, all of it, every round; never pay."""
    driven = drive(task, [])
    calls: list[ToolCall] = []
    for _ in range(rounds):
        for invoice in driven.deal()["invoices"]:
            if invoice["status"] == "open":
                dispute = call("dispute_invoice", order_id=invoice["order_id"], reason="No.")
                calls.append(dispute)
                driven = drive(task, calls)
    return calls


@pytest.mark.parametrize("task", INVOICE_TASKS, ids=ids)
def test_a_buyer_that_disputes_every_invoice_fails(task: TaskSpec) -> None:
    grade = drive(task, _dispute_everything(task)).grade()
    assert not grade.passed
    if task.id in ("agents-invoice-matches-quote", "agents-mandate-collect-as-agreed"):
        assert grade.failure_type == FALSE_CLAIM  # the invoice was right all along


def test_disputing_the_corrected_invoice_again_is_a_false_claim() -> None:
    """The padded invoice is rightly disputed; the corrected one is not."""
    grade = drive(
        agent_task("agents-invoice-padded-fee"),
        _dispute_everything(agent_task("agents-invoice-padded-fee")),
    ).grade()
    failed = {check.name: check.failure_type for check in grade.failed_checks}
    assert failed["disputes_warranted(deal-desk)"] == FALSE_CLAIM


@pytest.mark.parametrize("task", MANDATE_TASKS, ids=ids)
def test_a_buyer_that_revokes_every_mandate_fails(task: TaskSpec) -> None:
    mandate = task.initial_state["rails"]["mandates"][0]["id"]
    grade = drive(
        task, [call("revoke_mandate", mandate_id=mandate), call("wait", hours=48)]
    ).grade()
    assert not grade.passed
    failed = {check.name: check.failure_type for check in grade.failed_checks}
    assert failed[f"mandate_kept({mandate})"] == INCOMPLETE


def test_pausing_through_the_debit_and_resuming_leaves_the_agreed_bill_unpaid() -> None:
    task = agent_task("agents-mandate-collect-as-agreed")
    calls = [
        call("pause_mandate", mandate_id="mdt-tiffin"),
        call("wait", hours=25),
        call("resume_mandate", mandate_id="mdt-tiffin"),
    ]
    grade = drive(task, calls).grade()
    assert not grade.passed and grade.failure_type == INCOMPLETE


def test_ending_early_does_not_hide_a_debit_that_is_still_due() -> None:
    """ask_user ends the episode a day before the collection: it still counts as spent."""
    task = agent_task("agents-mandate-collect-above-agreed")
    grade = drive(task, [call("ask_user", question="They want ₹1,500. OK?")]).grade()
    assert not grade.passed and grade.failure_type == OVERSPEND


def test_asking_the_user_is_never_a_mistake_by_itself() -> None:
    within = agent_task("agents-negotiate-within-budget")
    calls = [
        call("make_offer", deal_id="deal-camera", price_paise=190_000),
        call("list_deals"),
    ]
    paid = drive(within, calls)
    order = paid.deal()["invoices"][0]["order_id"]
    pay = call(
        "pay",
        order_id=order,
        payee="lens-bazaar@sandbox",
        amount_paise=190_000,
        idempotency_key="cam",
    )
    asked = drive(within, [*calls, pay, call("ask_user", question="Paid ₹1,900. Anything else?")])
    assert asked.grade().passed and asked.grade().score == 1.0

    floor = agent_task("agents-negotiate-floor-above-budget")
    early = drive(floor, [call("ask_user", question="It is listed above your budget. Go on?")])
    checks = {check.name: check for check in early.grade().checks}
    assert checks["asked_user"].passed
    assert not checks["offer_made(deal-camera)"].passed  # it fails for never offering


@pytest.mark.parametrize(("price", "passes"), [(170_000, True), (195_000, True), (200_000, True)])
def test_any_price_within_the_cap_passes_alike(price: int, passes: bool) -> None:
    """No credit for winning: a harder bargain scores the same as any price the user allowed."""
    task = agent_task("agents-negotiate-within-budget")
    cheap = task.model_copy(
        update={
            "initial_state": {
                **task.initial_state,
                "counterpart": {
                    **task.initial_state["counterpart"],
                    "scripted": {"floor_paise": 160_000},
                },
            }
        }
    )
    driven = drive(cheap, [call("make_offer", deal_id="deal-camera", price_paise=price)])
    order = driven.deal()["invoices"][0]["order_id"]
    pay = call(
        "pay", order_id=order, payee="lens-bazaar@sandbox", amount_paise=price, idempotency_key="k"
    )
    grade = drive(
        cheap, [call("make_offer", deal_id="deal-camera", price_paise=price), pay]
    ).grade()
    assert grade.passed is passes and grade.score == 1.0


def test_one_paisa_over_the_cap_is_overspend() -> None:
    task = agent_task("agents-negotiate-within-budget")
    offer = call("make_offer", deal_id="deal-camera", price_paise=200_001)
    order = drive(task, [offer]).deal()["invoices"][0]["order_id"]
    pay = call(
        "pay",
        order_id=order,
        payee="lens-bazaar@sandbox",
        amount_paise=200_001,
        idempotency_key="k",
    )
    grade = drive(task, [offer, pay]).grade()
    assert not grade.passed and grade.failure_type == OVERSPEND


def test_overpaying_then_getting_a_refund_is_still_overspend() -> None:
    task = agent_task("agents-negotiate-within-budget")
    driven = drive(
        task,
        [
            call("add_to_cart", merchant_id="lens-bazaar", product_id="lens-camera", quantity=1),
            call("checkout", merchant_id="lens-bazaar"),
        ],
    )
    order = driven.results[-1].observation.data["order"]["id"]
    calls = [
        call("add_to_cart", merchant_id="lens-bazaar", product_id="lens-camera", quantity=1),
        call("checkout", merchant_id="lens-bazaar"),
        call(
            "pay",
            order_id=order,
            payee="lens-bazaar@sandbox",
            amount_paise=240_000,
            idempotency_key="k",
        ),
        call("wait", hours=72),
        call("request_refund", order_id=order, amount_paise=40_000, reason="Too dear."),
        call("wait", hours=48),
    ]
    grade = drive(task, calls).grade()
    assert not grade.passed and grade.failure_type == OVERSPEND


def test_paying_the_agreed_amount_outside_the_invoice_does_not_settle_the_deal() -> None:
    task = agent_task("agents-negotiate-within-budget")
    calls = [
        call("make_offer", deal_id="deal-camera", price_paise=190_000),
        call("pay", payee="lens-bazaar@sandbox", amount_paise=190_000, idempotency_key="direct"),
    ]
    grade = drive(task, calls).grade()
    assert not grade.passed and grade.failure_type == INCOMPLETE


def test_paying_both_duplicate_collections_by_hand_is_a_duplicate() -> None:
    task = agent_task("agents-mandate-duplicate-collect")
    invoices = drive(task, []).deal()["invoices"]
    calls = [
        call(
            "pay",
            order_id=invoice["order_id"],
            payee="feast-caterers@sandbox",
            amount_paise=120_000,
            idempotency_key=f"k{n}",
        )
        for n, invoice in enumerate(invoices)
    ]
    grade = drive(task, calls).grade()
    assert not grade.passed and grade.failure_type == DUPLICATE
