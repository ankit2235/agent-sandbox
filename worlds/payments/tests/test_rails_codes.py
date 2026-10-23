"""One scenario per failure code, and a check that no code is left without one."""

from collections.abc import Callable

import pytest
from rails_testkit import ALICE, BOB, SHOP, Harness, config_dict

from kit.errors import ToolRejected
from worlds.payments.services.rails.models import Decline, MandateRequest, Rejection

ICE = "ice@sandbox"


def declined(result) -> str:
    body = result.body
    record = body.get("payment") or body.get("refund")
    assert record["status"] == "failed", record
    return record["failure_code"]


def rejected(call: Callable[[], object]) -> str:
    with pytest.raises(ToolRejected) as info:
        call()
    return info.value.code


def with_mandate(**mandate: object) -> Harness:
    base = {"id": "m1", "payer": ALICE, "payee": SHOP, "max_amount_paise": 10_000}
    base.update(mandate)
    return Harness(config_dict(mandates=[base]))


def with_limits(**limits: object) -> Harness:
    return Harness(config_dict(limits={ALICE: limits}))


# ------------------------------------------------------------------ declines
def insufficient_funds() -> str:
    return declined(Harness().pay(200_000, payer=BOB))


def payee_not_found() -> str:
    return declined(Harness().pay(payee="ghost@sandbox"))


def account_frozen() -> str:
    accounts = [*config_dict()["accounts"], {"handle": ICE, "holder": "Ice", "status": "frozen"}]
    return declined(Harness(config_dict(accounts=accounts)).pay(payee=ICE))


def limit_per_txn_exceeded() -> str:
    return declined(with_limits(per_txn_paise=50_000).pay(60_000))


def limit_daily_exceeded() -> str:
    h = with_limits(daily_paise=100_000)
    h.pay(60_000)
    return declined(h.pay(60_000))


def limit_monthly_exceeded() -> str:
    h = with_limits(monthly_paise=100_000)
    h.pay(60_000)
    h.advance(2 * 86_400)
    return declined(h.pay(60_000))


def limit_merchant_exceeded() -> str:
    h = with_limits(per_merchant_daily_paise={SHOP: 30_000})
    h.pay(20_000)
    return declined(h.pay(20_000))


def mandate_not_found() -> str:
    return declined(Harness().pay(mandate_id="nope"))


def mandate_mismatch() -> str:
    return declined(with_mandate(type="one_time").pay(payee=BOB, mandate_id="m1"))


def mandate_not_started() -> str:
    return declined(with_mandate(type="one_time", start_offset_seconds=3600).pay(mandate_id="m1"))


def mandate_expired() -> str:
    h = with_mandate(type="one_time", end_offset_seconds=60)
    h.advance(120)
    return declined(h.pay(mandate_id="m1"))


def mandate_revoked() -> str:
    return declined(with_mandate(type="one_time", status="revoked").pay(mandate_id="m1"))


def mandate_paused() -> str:
    return declined(with_mandate(type="one_time", status="paused").pay(mandate_id="m1"))


def mandate_amount_exceeded() -> str:
    h = with_mandate(type="recurring", frequency="monthly")
    return declined(h.pay(20_000, mandate_id="m1"))


def mandate_limit_exhausted() -> str:
    h = with_mandate(type="one_time")
    h.pay(5_000, mandate_id="m1")
    return declined(h.pay(5_000, mandate_id="m1"))


def mandate_frequency_exceeded() -> str:
    h = with_mandate(type="recurring", frequency="monthly")
    h.pay(5_000, mandate_id="m1")
    return declined(h.pay(5_000, mandate_id="m1"))


def reversed_after_timeout() -> str:
    h = Harness(config_dict(faults={"timeout_on_attempts": [1], "timeout_outcome": "reversed"}))
    payment = h.pay().body["payment"]
    h.advance(90)
    stored = h.stored(payment["id"])
    assert stored["status"] == "reversed"
    return stored["failure_code"]


def refund_not_allowed() -> str:
    h = Harness()
    failed = h.pay(200_000, payer=BOB).body["payment"]
    return declined(h.refund(failed["id"], 1_000))


def refund_exceeds_payment() -> str:
    h = Harness()
    paid = h.pay(10_000).body["payment"]
    return declined(h.refund(paid["id"], 10_001))


# ---------------------------------------------------------------- rejections
def invalid_request() -> str:
    return rejected(lambda: Harness().admin("payments", colour="blue"))


def invalid_amount() -> str:
    return rejected(lambda: Harness().pay(0))


def same_account() -> str:
    return rejected(lambda: Harness().pay(payee=ALICE))


def payer_not_found() -> str:
    return rejected(lambda: Harness().pay(payer="ghost@sandbox"))


def idempotency_conflict() -> str:
    h = Harness()
    h.pay(10_000, key="k-1")
    return rejected(lambda: h.pay(10_001, key="k-1"))


def account_not_found() -> str:
    return rejected(lambda: Harness().rails.account("ghost@sandbox"))


def mandate_not_found_rejection() -> str:
    h = Harness()
    return rejected(lambda: h.rails.mandate_action("nope", "pause", h.now))


def payment_not_found() -> str:
    h = Harness()
    return rejected(lambda: h.rails.payment("pay_nope", h.now))


def refund_not_found() -> str:
    return rejected(lambda: Harness().rails.refund("rfd_nope"))


def invalid_transition() -> str:
    h = with_mandate(type="one_time")
    return rejected(lambda: h.rails.mandate_action("m1", "resume", h.now))


def already_loaded() -> str:
    h = Harness()
    return rejected(lambda: h.rails.load(h.config, h.now))


def unknown_event() -> str:
    h = Harness()
    return rejected(lambda: h.rails.apply_event("rails.meteor", {}, h.now))


def unknown_resource() -> str:
    return rejected(lambda: Harness().admin("secrets"))


def mandate_request(**fields: object) -> MandateRequest:
    base: dict[str, object] = {
        "payer": ALICE,
        "payee": SHOP,
        "type": "reserve",
        "max_amount_paise": 10_000,
        "valid_seconds": 3_600,
    }
    base.update(fields)
    return MandateRequest.model_validate(base)


def invalid_mandate() -> str:
    h = Harness()
    return rejected(lambda: h.rails.create_mandate(mandate_request(valid_seconds=None), h.now))


def insufficient_funds_for_a_block() -> str:
    h = Harness()
    request = mandate_request(payer=BOB, max_amount_paise=100_001)
    return rejected(lambda: h.rails.create_mandate(request, h.now))


DECLINES: dict[Decline, Callable[[], str]] = {
    Decline.INSUFFICIENT_FUNDS: insufficient_funds,
    Decline.PAYEE_NOT_FOUND: payee_not_found,
    Decline.ACCOUNT_FROZEN: account_frozen,
    Decline.LIMIT_PER_TXN_EXCEEDED: limit_per_txn_exceeded,
    Decline.LIMIT_DAILY_EXCEEDED: limit_daily_exceeded,
    Decline.LIMIT_MONTHLY_EXCEEDED: limit_monthly_exceeded,
    Decline.LIMIT_MERCHANT_EXCEEDED: limit_merchant_exceeded,
    Decline.MANDATE_NOT_FOUND: mandate_not_found,
    Decline.MANDATE_MISMATCH: mandate_mismatch,
    Decline.MANDATE_NOT_STARTED: mandate_not_started,
    Decline.MANDATE_EXPIRED: mandate_expired,
    Decline.MANDATE_REVOKED: mandate_revoked,
    Decline.MANDATE_PAUSED: mandate_paused,
    Decline.MANDATE_AMOUNT_EXCEEDED: mandate_amount_exceeded,
    Decline.MANDATE_LIMIT_EXHAUSTED: mandate_limit_exhausted,
    Decline.MANDATE_FREQUENCY_EXCEEDED: mandate_frequency_exceeded,
    Decline.REVERSED_AFTER_TIMEOUT: reversed_after_timeout,
    Decline.REFUND_NOT_ALLOWED: refund_not_allowed,
    Decline.REFUND_EXCEEDS_PAYMENT: refund_exceeds_payment,
}

REJECTIONS: dict[Rejection, Callable[[], str]] = {
    Rejection.INVALID_REQUEST: invalid_request,
    Rejection.INVALID_AMOUNT: invalid_amount,
    Rejection.SAME_ACCOUNT: same_account,
    Rejection.PAYER_NOT_FOUND: payer_not_found,
    Rejection.IDEMPOTENCY_CONFLICT: idempotency_conflict,
    Rejection.ACCOUNT_NOT_FOUND: account_not_found,
    Rejection.MANDATE_NOT_FOUND: mandate_not_found_rejection,
    Rejection.PAYMENT_NOT_FOUND: payment_not_found,
    Rejection.REFUND_NOT_FOUND: refund_not_found,
    Rejection.INVALID_TRANSITION: invalid_transition,
    Rejection.ALREADY_LOADED: already_loaded,
    Rejection.UNKNOWN_EVENT: unknown_event,
    Rejection.UNKNOWN_RESOURCE: unknown_resource,
    Rejection.INVALID_MANDATE: invalid_mandate,
    Rejection.INSUFFICIENT_FUNDS: insufficient_funds_for_a_block,
}


@pytest.mark.parametrize("code", list(DECLINES), ids=str)
def test_every_decline_code(code: Decline) -> None:
    assert DECLINES[code]() == code


@pytest.mark.parametrize("code", list(REJECTIONS), ids=str)
def test_every_rejection_code(code: Rejection) -> None:
    assert REJECTIONS[code]() == code


def test_no_code_is_left_without_a_scenario() -> None:
    assert set(DECLINES) == set(Decline)
    assert set(REJECTIONS) == set(Rejection)


def test_a_refund_the_merchant_cannot_cover_is_declined() -> None:
    h = Harness()
    paid = h.pay(10_000).body["payment"]
    h.pay(10_000, payer=SHOP, payee=BOB)  # the shop spends the money first
    assert declined(h.refund(paid["id"], 5_000)) == Decline.INSUFFICIENT_FUNDS
