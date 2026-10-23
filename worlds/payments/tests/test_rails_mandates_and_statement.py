"""Phase 2 additions to the rails: creating mandates, the statement, a payer's mandates."""

import pytest
from rails_testkit import ALICE, BOB, SHOP, Harness, check_books

from kit.errors import ToolRejected
from worlds.payments.services.rails.core import HOLDS
from worlds.payments.services.rails.models import MandateRequest

OPENING = 600_000


def request(**fields: object) -> MandateRequest:
    base: dict[str, object] = {
        "payer": ALICE, "payee": SHOP, "type": "recurring", "max_amount_paise": 10_000,
        "frequency": "monthly",
    }  # fmt: skip
    base.update(fields)
    return MandateRequest.model_validate(base)


def test_a_created_recurring_mandate_can_be_debited_once_a_period() -> None:
    h = Harness()
    mandate = h.rails.create_mandate(request(), h.now).body["mandate"]
    assert mandate["id"].startswith("mdt_") and mandate["status"] == "active"
    assert mandate["remaining_limit_paise"] is None
    assert h.pay(10_000, mandate_id=mandate["id"]).body["payment"]["status"] == "success"
    second = h.pay(10_000, mandate_id=mandate["id"]).body["payment"]
    assert second["failure_code"] == "mandate_frequency_exceeded"
    check_books(h.rails, h.now, OPENING)


def test_a_created_reserve_mandate_blocks_funds_and_releases_them_on_expiry() -> None:
    h = Harness()
    result = h.schedule(
        h.rails.create_mandate(request(type="reserve", frequency=None, valid_seconds=3_600), h.now)
    )
    mandate = result.body["mandate"]
    assert h.balance(ALICE) == 490_000 and h.balance(HOLDS) == 10_000
    assert [event.name for event in result.scheduled] == ["rails.reserve_expiry"]
    h.pay(4_000, mandate_id=mandate["id"])
    h.advance(3_600)
    assert h.balance(ALICE) == 496_000 and h.balance(HOLDS) == 0
    check_books(h.rails, h.now, OPENING)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"max_amount_paise": 0}, "invalid_amount"),
        ({"payee": ALICE}, "same_account"),
        ({"frequency": None}, "invalid_mandate"),
        ({"type": "one_time"}, "invalid_mandate"),
        ({"payer": "ghost@sandbox"}, "payer_not_found"),
        ({"payee": "ghost@sandbox"}, "account_not_found"),
        ({"type": "reserve", "frequency": None, "valid_seconds": 91 * 86_400}, "invalid_mandate"),
        ({"type": "reserve", "frequency": None, "valid_seconds": 60,
          "max_amount_paise": 1_000_001}, "invalid_mandate"),
        ({"type": "reserve", "frequency": None, "valid_seconds": 60, "payer": BOB,
          "max_amount_paise": 100_001}, "insufficient_funds"),
    ],
)  # fmt: skip
def test_mandate_requests_that_break_a_rule_are_refused_and_record_nothing(
    fields: dict, code: str
) -> None:
    h = Harness()
    with pytest.raises(ToolRejected) as info:
        h.rails.create_mandate(request(**fields), h.now)
    assert info.value.code == code
    assert h.admin("mandates") == []


def test_the_statement_is_newest_first_with_refunds_and_filters() -> None:
    h = Harness()
    paid = h.pay(10_000).body["payment"]
    h.advance(10)
    h.pay(900_000)  # declined: insufficient funds
    h.advance(10)
    h.refund(paid["id"], 4_000)
    h.advance(10)
    h.pay(2_000, payer=BOB, payee=ALICE)
    entries = h.rails.transactions(ALICE, h.now)
    assert [(e["kind"], e["direction"], e["amount_paise"], e["status"]) for e in entries] == [
        ("payment", "credit", 2_000, "success"),
        ("refund", "credit", 4_000, "success"),
        ("payment", "debit", 900_000, "failed"),
        ("payment", "debit", 10_000, "success"),
    ]
    assert entries[1]["payment_id"] == paid["id"]
    assert len(h.rails.transactions(ALICE, h.now, limit=2)) == 2
    assert [e["amount_paise"] for e in h.rails.transactions(ALICE, h.now, status="failed")] == [
        900_000
    ]
    shop = h.rails.transactions(SHOP, h.now)
    assert [(e["kind"], e["direction"]) for e in shop] == [("refund", "debit"),
                                                          ("payment", "credit")]  # fmt: skip


def test_the_statement_lags_like_a_status_check() -> None:
    h = Harness({**Harness().config.model_dump(mode="json"), "faults": {"status_lag_seconds": 60}})
    h.pay(10_000)
    assert h.rails.transactions(ALICE, h.now)[0]["status"] == "pending"
    h.advance(60)
    assert h.rails.transactions(ALICE, h.now)[0]["status"] == "success"


def test_a_payers_mandates_only() -> None:
    h = Harness()
    h.rails.create_mandate(request(), h.now)
    h.rails.create_mandate(request(payer=BOB), h.now)
    assert [m["payer"] for m in h.rails.mandates_of(ALICE, h.now)] == [ALICE]
    with pytest.raises(ToolRejected):
        h.rails.mandates_of("ghost@sandbox", h.now)
