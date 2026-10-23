"""How the rails behave over time: timeouts, lag, idempotency, refunds, holds, limits."""

import datetime as dt

import pytest
from pydantic import ValidationError
from rails_testkit import ALICE, BOB, SHOP, Harness, check_books, config_dict

from kit.clock import EPOCH
from worlds.payments.services.rails import RailsConfig
from worlds.payments.services.rails.core import CLEARING, HOLDS, iso

OPENING = 600_000


def test_a_payment_moves_money_and_reports_exactly_what_changed() -> None:
    h = Harness()
    result = h.pay(25_000)
    payment = result.body["payment"]
    assert payment["status"] == "success"
    assert (h.balance(ALICE), h.balance(SHOP)) == (475_000, 25_000)
    paths = [entry.path for entry in result.state_diff]
    assert paths == [
        f"rails/payment/{payment['id']}",
        "rails/account/acc_alice/balance_paise",
        "rails/account/acc_shop/balance_paise",
    ]
    assert result.state_diff[1].before == 500_000
    assert result.state_diff[1].after == 475_000
    check_books(h.rails, h.now, OPENING)


def test_a_declined_payment_is_recorded_but_moves_nothing() -> None:
    h = Harness()
    result = h.pay(200_000, payer=BOB)
    assert [entry.op for entry in result.state_diff] == ["create"]
    assert h.balance(BOB) == 100_000
    assert h.stored(result.body["payment"]["id"])["failure_detail"] == {
        "amount_paise": 200_000,
        "balance_paise": 100_000,
    }


# ------------------------------------------------------------- idempotency
def test_the_same_key_never_charges_twice() -> None:
    h = Harness()
    first = h.pay(10_000, key="order-42")
    again = h.pay(10_000, key="order-42")
    assert again.body["replayed"] is True
    assert again.body["payment"]["id"] == first.body["payment"]["id"]
    assert again.state_diff == []
    assert h.balance(ALICE) == 490_000
    assert len(h.admin("payments")) == 1


def test_a_replay_returns_the_original_failure_even_after_a_top_up() -> None:
    h = Harness()
    h.pay(200_000, payer=BOB, key="big")
    h.pay(150_000, payer=ALICE, payee=BOB)
    retry = h.pay(200_000, payer=BOB, key="big")
    assert retry.body["payment"]["failure_code"] == "insufficient_funds"


def test_a_rejected_request_is_not_recorded_so_its_key_stays_free() -> None:
    h = Harness()
    with pytest.raises(Exception, match="invalid_amount"):
        h.pay(-5, key="fresh")
    assert h.pay(5, key="fresh").body["payment"]["status"] == "success"


# -------------------------------------------------------------- timeouts
def test_a_timed_out_payment_really_went_through() -> None:
    h = Harness(config_dict(faults={"timeout_on_attempts": [1]}))
    result = h.pay(10_000, key="did-it-go")
    payment = result.body["payment"]
    assert result.body["timed_out"] is True
    assert payment["status"] == "pending"
    assert h.balance(ALICE) == 490_000
    assert h.balance(CLEARING) == 10_000
    assert [event.name for event in h.clock.pending] == ["rails.settle"]
    check_books(h.rails, h.now, OPENING)

    h.advance(89)
    assert h.rails.payment_by_key("did-it-go", h.now)["status"] == "pending"
    h.advance(1)
    assert h.rails.payment_by_key("did-it-go", h.now)["status"] == "success"
    assert (h.balance(CLEARING), h.balance(SHOP)) == (0, 10_000)
    check_books(h.rails, h.now, OPENING)


def test_only_the_scripted_attempt_times_out() -> None:
    h = Harness(config_dict(faults={"timeout_on_attempts": [2]}))
    assert [h.pay().body["timed_out"] for _ in range(3)] == [False, True, False]


def test_a_reversed_timeout_gives_the_money_and_the_mandate_allowance_back() -> None:
    mandate = {
        "id": "m1",
        "payer": ALICE,
        "payee": SHOP,
        "type": "recurring",
        "frequency": "monthly",
        "max_amount_paise": 10_000,
        "remaining_limit_paise": 30_000,
    }
    h = Harness(
        config_dict(
            mandates=[mandate], faults={"timeout_on_attempts": [1], "timeout_outcome": "reversed"}
        )
    )
    h.pay(10_000, mandate_id="m1")
    assert h.rails.mandate("m1", h.now)["remaining_limit_paise"] == 20_000
    h.advance(90)
    assert h.balance(ALICE) == 500_000
    assert h.rails.mandate("m1", h.now)["remaining_limit_paise"] == 30_000
    check_books(h.rails, h.now, OPENING)


def test_seeded_timeouts_repeat_exactly_for_the_same_seed() -> None:
    def pattern(seed: int) -> list[bool]:
        h = Harness(config_dict(faults={"timeout_rate": 0.5}), seed=seed)
        return [h.pay(100).body["timed_out"] for _ in range(20)]

    assert pattern(11) == pattern(11)
    assert 0 < sum(pattern(11)) < 20
    assert pattern(11) != pattern(12)


# ------------------------------------------------------------- status lag
def test_status_checks_lag_but_the_graders_see_the_truth() -> None:
    h = Harness(config_dict(faults={"status_lag_seconds": 300}))
    result = h.pay(10_000)
    payment_id = result.body["payment"]["id"]
    assert result.body["payment"]["status"] == "success"  # the reply itself is honest
    h.advance(299)
    assert h.rails.payment(payment_id, h.now)["status"] == "pending"
    assert h.stored(payment_id)["status"] == "success"
    h.advance(1)
    assert h.rails.payment(payment_id, h.now)["status"] == "success"


def test_lag_starts_after_a_timed_out_payment_settles() -> None:
    faults = {"timeout_on_attempts": [1], "settle_after_seconds": 60, "status_lag_seconds": 30}
    h = Harness(config_dict(faults=faults))
    payment_id = h.pay().body["payment"]["id"]
    h.advance(60)
    assert h.stored(payment_id)["status"] == "success"
    assert h.rails.payment(payment_id, h.now)["status"] == "pending"
    h.advance(30)
    assert h.rails.payment(payment_id, h.now)["status"] == "success"


# ---------------------------------------------------------------- refunds
def test_partial_refunds_add_up_to_the_payment_and_no_further() -> None:
    h = Harness()
    payment_id = h.pay(10_000).body["payment"]["id"]
    assert h.refund(payment_id, 4_000).body["refund"]["status"] == "success"
    assert h.refund(payment_id, 6_000).body["refund"]["status"] == "success"
    third = h.refund(payment_id, 1).body["refund"]
    assert third["failure_code"] == "refund_exceeds_payment"
    assert h.rails.payment(payment_id, h.now)["refunded_paise"] == 10_000
    assert h.balance(ALICE) == 500_000
    check_books(h.rails, h.now, OPENING)


def test_a_refund_retried_with_its_key_is_not_paid_twice() -> None:
    h = Harness()
    payment_id = h.pay(10_000).body["payment"]["id"]
    first = h.refund(payment_id, 5_000, key="rf-1")
    again = h.refund(payment_id, 5_000, key="rf-1")
    assert again.body["replayed"] is True
    assert again.body["refund"]["id"] == first.body["refund"]["id"]
    assert h.rails.payment(payment_id, h.now)["refunded_paise"] == 5_000
    with pytest.raises(Exception, match="idempotency_conflict"):
        h.refund(payment_id, 4_000, key="rf-1")


# --------------------------------------------------------- reserve blocks
def reserve_world(**mandate: object) -> Harness:
    base = {
        "id": "block",
        "payer": BOB,
        "payee": SHOP,
        "type": "reserve",
        "max_amount_paise": 60_000,
        "end_offset_seconds": 7 * 86_400,
    }
    base.update(mandate)
    return Harness(config_dict(mandates=[base]))


def test_a_reserve_block_holds_the_money_and_debits_come_from_it() -> None:
    h = reserve_world()
    assert (h.balance(BOB), h.balance(HOLDS)) == (40_000, 60_000)
    first = h.pay(50_000, payer=BOB, mandate_id="block").body["payment"]
    assert first["status"] == "success"  # more than Bob's free balance, but it was blocked
    assert (h.balance(BOB), h.balance(HOLDS), h.balance(SHOP)) == (40_000, 10_000, 50_000)
    over = h.pay(20_000, payer=BOB, mandate_id="block").body["payment"]
    assert over["failure_code"] == "mandate_limit_exhausted"
    check_books(h.rails, h.now, OPENING)


def test_revoking_a_block_releases_what_is_left() -> None:
    h = reserve_world()
    h.pay(15_000, payer=BOB, mandate_id="block")
    result = h.rails.mandate_action("block", "revoke", h.now)
    assert result.body["mandate"]["status"] == "revoked"
    assert (h.balance(BOB), h.balance(HOLDS)) == (85_000, 0)
    check_books(h.rails, h.now, OPENING)


def test_an_expired_block_is_released_by_a_scheduled_event() -> None:
    h = reserve_world(end_offset_seconds=3600)
    assert [event.name for event in h.clock.pending] == ["rails.reserve_expiry"]
    h.advance(3600)
    assert (h.balance(BOB), h.balance(HOLDS)) == (100_000, 0)
    assert h.rails.mandate("block", h.now)["status"] == "expired"
    check_books(h.rails, h.now, OPENING)


def test_a_block_that_times_out_and_reverses_after_expiry_goes_home() -> None:
    h = Harness(
        config_dict(
            mandates=[
                {
                    "id": "block",
                    "payer": BOB,
                    "payee": SHOP,
                    "type": "reserve",
                    "max_amount_paise": 60_000,
                    "end_offset_seconds": 60,
                }
            ],
            faults={"timeout_on_attempts": [1], "timeout_outcome": "reversed"},
        )
    )
    h.pay(20_000, payer=BOB, mandate_id="block")
    h.advance(90)  # expiry (60 s) and settlement (90 s) both fire in this step
    assert (h.balance(BOB), h.balance(HOLDS), h.balance(CLEARING)) == (100_000, 0, 0)
    check_books(h.rails, h.now, OPENING)


def test_reserve_blocks_respect_the_configured_caps() -> None:
    with pytest.raises(ValidationError, match="reserve_max_block_paise"):
        RailsConfig.model_validate(
            config_dict(
                mandates=[
                    {
                        "id": "big",
                        "payer": ALICE,
                        "payee": SHOP,
                        "type": "reserve",
                        "max_amount_paise": 1_000_001,
                        "end_offset_seconds": 60,
                    }
                ]
            )
        )
    with pytest.raises(ValidationError, match="cannot fund"):
        RailsConfig.model_validate(
            config_dict(
                mandates=[
                    {
                        "id": "big",
                        "payer": BOB,
                        "payee": SHOP,
                        "type": "reserve",
                        "max_amount_paise": 100_001,
                        "end_offset_seconds": 60,
                    }
                ]
            )
        )


# ---------------------------------------------------------------- mandates
def test_pause_and_resume_a_mandate() -> None:
    mandate = {
        "id": "m1",
        "payer": ALICE,
        "payee": SHOP,
        "type": "recurring",
        "frequency": "daily",
        "max_amount_paise": 10_000,
    }
    h = Harness(config_dict(mandates=[mandate]))
    h.rails.mandate_action("m1", "pause", h.now)
    assert h.pay(mandate_id="m1").body["payment"]["failure_code"] == "mandate_paused"
    h.rails.mandate_action("m1", "resume", h.now)
    assert h.pay(mandate_id="m1").body["payment"]["status"] == "success"


def test_a_recurring_mandate_allows_one_debit_per_calendar_period() -> None:
    mandate = {
        "id": "m1",
        "payer": ALICE,
        "payee": SHOP,
        "type": "recurring",
        "frequency": "weekly",
        "max_amount_paise": 10_000,
    }
    h = Harness(config_dict(mandates=[mandate]))  # EPOCH is a Thursday
    assert h.pay(mandate_id="m1").body["payment"]["status"] == "success"
    refused = h.pay(mandate_id="m1").body["payment"]
    assert refused["failure_code"] == "mandate_frequency_exceeded"
    h.advance(4 * 86_400)  # into Monday of the next week (IST)
    assert h.pay(mandate_id="m1").body["payment"]["status"] == "success"


# ------------------------------------------------------------------ limits
def test_a_limit_change_scheduled_by_the_task_applies_mid_episode() -> None:
    change = {
        "after_seconds": 600,
        "name": "rails.limit_change",
        "payload": {"handle": ALICE, "per_txn_paise": 5_000},
    }
    h = Harness(config_dict(scheduled=[change]))
    assert h.pay(10_000).body["payment"]["status"] == "success"
    [applied] = h.advance(600)
    assert applied.state_diff[0].path == "rails/limits/alice@sandbox"
    assert h.pay(10_000).body["payment"]["failure_code"] == "limit_per_txn_exceeded"


def test_a_rolling_window_says_when_room_comes_back() -> None:
    h = Harness(config_dict(limits={ALICE: {"daily_paise": 100_000}}))
    first_at = h.now
    h.pay(70_000)
    h.advance(3600)
    detail = h.stored(h.pay(50_000).body["payment"]["id"])["failure_detail"]
    assert detail == {
        "limit": "daily",
        "cap_paise": 100_000,
        "used_paise": 70_000,
        "remaining_paise": 30_000,
        "resets_at": iso(first_at + dt.timedelta(days=1)),
    }
    h.advance(23 * 3600)
    assert h.pay(50_000).body["payment"]["status"] == "success"


def test_a_calendar_day_resets_at_midnight_india_time() -> None:
    config = config_dict(
        limits={ALICE: {"daily_paise": 100_000}}, settings={"daily_window": "calendar_day"}
    )
    h = Harness(config)  # EPOCH is 09:00 UTC = 14:30 IST
    h.pay(70_000)
    detail = h.stored(h.pay(50_000).body["payment"]["id"])["failure_detail"]
    midnight_ist = dt.datetime(2026, 1, 1, 18, 30, tzinfo=dt.UTC)
    assert detail["resets_at"] == iso(midnight_ist)
    h.advance((midnight_ist - EPOCH).total_seconds())
    assert h.pay(50_000).body["payment"]["status"] == "success"


# ------------------------------------------------------------------ config
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"accounts": [{"handle": "alice@okaxis", "holder": "A"}]}, "should match pattern"),
        ({"accounts": [{"handle": ALICE, "holder": "A", "balance_paise": 10.5}]}, "integer"),
        ({"accounts": [{"handle": ALICE, "holder": "A", "balance_paise": "100"}]}, "integer"),
        (
            {"accounts": [{"handle": ALICE, "holder": "A"}, {"handle": ALICE, "holder": "B"}]},
            "unique",
        ),
        ({"limits": {"ghost@sandbox": {"daily_paise": 1}}}, "unknown handles"),
        (
            {
                "mandates": [
                    {
                        "id": "m",
                        "payer": ALICE,
                        "payee": SHOP,
                        "type": "recurring",
                        "max_amount_paise": 1,
                    }
                ]
            },
            "frequency",
        ),
        (
            {
                "mandates": [
                    {
                        "id": "m",
                        "payer": ALICE,
                        "payee": SHOP,
                        "type": "reserve",
                        "max_amount_paise": 1,
                    }
                ]
            },
            "needs an end",
        ),
        ({"faults": {"timeout_rate": 1.5}}, "less than or equal"),
    ],
)
def test_a_bad_task_config_is_refused_before_anything_loads(change: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        RailsConfig.model_validate(config_dict(**change))
