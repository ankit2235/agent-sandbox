"""Property tests: thousands of random action sequences, and the books always balance."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from rails_testkit import ALICE, BOB, SHOP, Harness, check_books, config_dict

from kit.errors import ToolRejected

CAROL = "carol@sandbox"
HANDLES = [ALICE, BOB, CAROL, SHOP]
MANDATES = {
    "sub": (ALICE, SHOP),
    "block": (ALICE, SHOP),
    "once": (BOB, SHOP),
}
OPENING = 1_000_000
EXAMPLES = settings(max_examples=1_000, deadline=None)


def world(outcome: str) -> dict[str, Any]:
    accounts = [
        {"handle": ALICE, "holder": "Alice Example", "balance_paise": 600_000},
        {"handle": BOB, "holder": "Bob Example", "balance_paise": 250_000},
        {"handle": CAROL, "holder": "Carol Example", "balance_paise": 150_000},
        {"handle": SHOP, "holder": "Example Shop", "kind": "merchant"},
    ]
    return config_dict(
        accounts=accounts,
        mandates=[
            {
                "id": "sub",
                "payer": ALICE,
                "payee": SHOP,
                "type": "recurring",
                "frequency": "weekly",
                "max_amount_paise": 50_000,
                "remaining_limit_paise": 120_000,
            },
            {
                "id": "block",
                "payer": ALICE,
                "payee": SHOP,
                "type": "reserve",
                "max_amount_paise": 200_000,
                "end_offset_seconds": 3 * 86_400,
            },
            {
                "id": "once",
                "payer": BOB,
                "payee": SHOP,
                "type": "one_time",
                "max_amount_paise": 30_000,
            },
        ],
        limits={
            ALICE: {"daily_paise": 400_000, "per_merchant_daily_paise": {SHOP: 250_000}},
            BOB: {"per_txn_paise": 90_000, "monthly_paise": 200_000},
        },
        faults={
            "timeout_rate": 0.3,
            "settle_after_seconds": 90,
            "status_lag_seconds": 30,
            "timeout_outcome": outcome,
        },
        scheduled=[
            {
                "after_seconds": 3600,
                "name": "rails.limit_change",
                "payload": {"handle": ALICE, "daily_paise": 150_000},
            }
        ],
    )


amounts = st.integers(min_value=-5, max_value=300_000)
pay = st.tuples(
    st.just("pay"),
    st.sampled_from(HANDLES),
    st.sampled_from([*HANDLES, "ghost@sandbox"]),
    amounts,
    st.integers(0, 15),
    st.sampled_from([None, None, *MANDATES]),
)
refund = st.tuples(st.just("refund"), st.integers(0, 30), amounts, st.integers(0, 15))
advance = st.tuples(st.just("advance"), st.integers(1, 100_000))
mandate = st.tuples(
    st.just("mandate"),
    st.sampled_from(list(MANDATES)),
    st.sampled_from(["pause", "resume", "revoke"]),
)
operations = st.lists(st.one_of(pay, refund, advance, mandate), max_size=40)
outcomes = st.sampled_from(["success", "reversed"])


def run(h: Harness, op: tuple[Any, ...]) -> None:
    """Apply one random operation. Refusals are expected; anything else is a bug."""
    kind = op[0]
    try:
        if kind == "pay":
            _, payer, payee, amount, key, mandate_id = op
            if mandate_id is not None:
                payer, payee = MANDATES[mandate_id]
            h.pay(amount, payer=payer, payee=payee, key=f"p{key}", mandate_id=mandate_id)
        elif kind == "refund":
            _, index, amount, key = op
            payments = h.admin("payments")
            payment_id = payments[index % len(payments)]["id"] if payments else "pay_none"
            h.refund(payment_id, amount, key=f"r{key}")
        elif kind == "advance":
            h.advance(op[1])
        else:
            h.rails.mandate_action(op[1], op[2], h.now)
    except ToolRejected:
        pass


@EXAMPLES
@given(ops=operations, outcome=outcomes)
def test_the_ledger_always_balances(ops: list[tuple[Any, ...]], outcome: str) -> None:
    h = Harness(world(outcome))
    check_books(h.rails, h.now, OPENING)
    for op in ops:
        run(h, op)
        check_books(h.rails, h.now, OPENING)
    h.advance(10 * 86_400)  # let every timeout settle and every block expire
    check_books(h.rails, h.now, OPENING)
    assert h.balance("sys_clearing") == 0
    assert h.balance("sys_holds") == 0


@EXAMPLES
@given(ops=operations, outcome=outcomes, replays=st.lists(st.integers(0, 15), max_size=10))
def test_one_key_is_one_payment_and_at_most_one_debit(
    ops: list[tuple[Any, ...]], outcome: str, replays: list[int]
) -> None:
    h = Harness(world(outcome))
    for op in ops:
        run(h, op)
    for key in replays:
        stored = h.admin("payments", idempotency_key=f"p{key}")
        if not stored:
            continue
        original = stored[0]
        before = h.rails.store.snapshot()
        replay = h.pay(
            original["amount_paise"],
            payer=original["payer"],
            payee=original["payee"],
            key=f"p{key}",
            mandate_id=original["mandate_id"],
        )
        assert replay.body["replayed"] is True
        assert replay.body["payment"]["id"] == original["id"]
        assert h.rails.store.snapshot() == before, "a replay must not write anything"

    payments = h.admin("payments")
    keys = [payment["idempotency_key"] for payment in payments]
    assert len(keys) == len(set(keys))
    ledger = h.admin("ledger")
    for payment in payments:
        debited = -sum(
            entry["delta_paise"]
            for entry in ledger
            if entry["txn_ref"] == payment["id"]
            and entry["account_id"] == payment["funded_from"]
            and entry["delta_paise"] < 0
        )
        expected = 0 if payment["status"] == "failed" else payment["amount_paise"]
        assert debited == expected, payment
