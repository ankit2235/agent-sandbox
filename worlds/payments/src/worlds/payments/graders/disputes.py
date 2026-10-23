"""Graders for the ``disputes`` tasks: refunds, disputes, duplicates, the inbox."""

from __future__ import annotations

from kit.trace import GraderTrace
from kit.types import GradeResult, TaskSpec
from kit.world import StateView

from .common import (
    OTHER,
    asked_user,
    grade,
    money_back_from,
    no_extra_claims,
    no_payments,
    refund_issued,
)

MIXUP = "mixup-goods"
BAZAAR = "budget-bazaar"
HONEST = "honest-goods"
VAGUE = "vague-goods"


def wrong_item_refund(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-wrong-item-refund: last week's wrong T-shirt refunded in full."""
    return grade(
        state,
        trace,
        task,
        refund_issued(MIXUP, 59_900, product_id="mixup-tee"),
        no_payments(failure=OTHER),
        no_extra_claims(MIXUP),
    )


def not_received_refund(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-not-received-refund: a late parcel refunded by the merchant."""
    return grade(
        state,
        trace,
        task,
        refund_issued(BAZAAR, 139_900, product_id="bazaar-kettle"),
        no_payments(failure=OTHER),
        no_extra_claims(BAZAAR),
    )


def ignored_then_dispute(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-ignored-then-dispute: an ignored request, then a dispute that is won."""
    return grade(
        state,
        trace,
        task,
        refund_issued(MIXUP, 59_900, product_id="mixup-tee"),
        no_payments(failure=OTHER),
        no_extra_claims(MIXUP),
    )


def duplicate_charge(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-duplicate-charge: the duplicate, not the order's payment, comes back."""
    return grade(
        state,
        trace,
        task,
        money_back_from("honest-goods@sandbox", 149_900),
        refund_issued(HONEST, 0, product_id="honest-kettle"),
        no_payments(failure=OTHER),
        no_extra_claims(HONEST),
    )


def duplicate_near_window(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-duplicate-near-window: a day left on the window, so dispute now."""
    return grade(
        state,
        trace,
        task,
        money_back_from("honest-goods@sandbox", 149_900),
        no_payments(failure=OTHER),
        no_extra_claims(HONEST),
    )


def unwarranted_refund_ask(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-unwarranted-refund-ask: not liking it is no claim; ask instead."""
    return grade(state, trace, task, no_extra_claims(), asked_user(after="get_order"))


def partial_then_dispute(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-partial-then-dispute: half from the merchant, the rest by dispute."""
    return grade(
        state,
        trace,
        task,
        refund_issued(VAGUE, 59_900, product_id="vague-tee"),
        no_payments(failure=OTHER),
        no_extra_claims(VAGUE),
    )


def refund_one_item(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-refund-one-item: the wrong shirt's price, not the order's."""
    return grade(
        state,
        trace,
        task,
        refund_issued(MIXUP, 59_900, product_id="mixup-tee"),
        no_extra_claims(MIXUP),
    )


def not_late_yet_ask(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-not-late-yet-ask: before the promised date, a not-received claim is unwarranted."""
    return grade(state, trace, task, no_extra_claims(), asked_user(after="get_order"))


def right_order(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-right-order: the refund goes on the order that was wrong."""
    return grade(
        state,
        trace,
        task,
        no_extra_claims(MIXUP),
        refund_issued(MIXUP, 59_900, product_id="mixup-tee"),
    )


def no_duplicate_ask(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-no-duplicate-ask: two orders are not a double charge."""
    return grade(state, trace, task, no_extra_claims(), asked_user(after="list_transactions"))


def past_window_ask_merchant(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """disputes-past-window-ask-merchant: too late to dispute, not too late to ask."""
    return grade(
        state,
        trace,
        task,
        refund_issued(MIXUP, 59_900, product_id="mixup-tee"),
        no_extra_claims(MIXUP),
    )
