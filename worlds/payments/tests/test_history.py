"""``history``: what the user did before the task, replayed when the task loads."""

import copy
import datetime as dt
from typing import Any

import pytest
from payments_testkit import DAY, HOUR, USER, Session, base_state, handle, make_task

from kit.clock import EPOCH
from worlds.payments.state import PaymentsState, shifted

HONEST = "honest-goods"


def with_history(*entries: dict[str, Any], **rails: Any) -> dict[str, Any]:
    state = base_state()
    state["history"] = list(entries)
    state["rails"].update(rails)
    return state


def kettle(seconds_ago: int, merchant: str = HONEST) -> dict[str, Any]:
    return {
        "kind": "purchase",
        "seconds_ago": seconds_ago,
        "merchant_id": merchant,
        "items": [{"product_id": f"{merchant}-kettle"}],
    }


def test_the_agent_starts_after_history_with_its_effects_in_place() -> None:
    session = Session(make_task(with_history(kettle(5 * DAY))))
    assert session.first.sim_time == EPOCH + dt.timedelta(days=5)
    assert session.episode.trace.steps == []
    (order,) = session.admin("commerce", "orders")
    assert order["status"] == "delivered"
    assert order["paid_at"] == "2026-01-01T09:00:00.000000Z"
    assert session.balance(USER) == 5_000_000 - 149_900
    subjects = [m["subject"] for m in session.admin("disputes", "messages")]
    assert any("Invoice" in s for s in subjects) and any("delivered" in s for s in subjects)


def test_entries_replay_oldest_first_whatever_their_order() -> None:
    state = with_history(
        {"kind": "payment", "seconds_ago": HOUR, "payee": handle(HONEST), "amount_paise": 100},
        {"kind": "payment", "seconds_ago": 3 * HOUR, "payee": handle(HONEST), "amount_paise": 200},
    )
    payments = Session(make_task(state)).admin("rails", "payments")
    assert [p["amount_paise"] for p in payments] == [200, 100]
    assert [p["created_at"][11:19] for p in payments] == ["09:00:00", "11:00:00"]


def test_reset_and_close_restore_the_state_after_history() -> None:
    session = Session(make_task(with_history(kettle(5 * DAY))))
    after_history = session.episode.snapshot_all()
    session.buy(HONEST, "tee", variant="M")
    assert session.episode.snapshot_all() != after_history
    session.episode.close()
    assert session.episode.snapshot_all() == after_history


def test_the_same_history_replays_byte_for_byte() -> None:
    task = make_task(with_history(kettle(5 * DAY), kettle(2 * DAY, "slow-goods")))
    assert Session(task).episode.snapshot_all() == Session(task).episode.snapshot_all()


def test_timeout_on_attempt_one_means_the_agents_first_payment() -> None:
    faults = {"timeout_on_attempts": [1]}
    session = Session(make_task(with_history(kettle(DAY), kettle(HOUR), faults=faults)))
    assert all(not p["timed_out"] for p in session.admin("rails", "payments"))
    paid = session.ok("pay", payee=handle(HONEST), amount_paise=500, idempotency_key="k1")
    assert paid["outcome"] == "timeout"
    again = session.ok("pay", payee=handle(HONEST), amount_paise=700, idempotency_key="k2")
    assert again["outcome"] == "paid"


def test_seeded_timeouts_do_not_depend_on_how_much_history_there_is() -> None:
    faults = {"timeout_rate": 0.5}

    def outcomes(state: dict[str, Any]) -> list[str]:
        session = Session(make_task(state))
        return [
            session.ok("pay", payee=handle(HONEST), amount_paise=100 + n, idempotency_key=f"k{n}")[
                "outcome"
            ]
            for n in range(12)
        ]

    plain = base_state()
    plain["rails"]["faults"] = faults
    seen = outcomes(plain)
    assert {"timeout", "paid"} <= set(seen)
    assert outcomes(with_history(kettle(DAY), faults=faults)) == seen


def test_offsets_count_from_when_the_agent_starts() -> None:
    state = with_history(
        kettle(10 * DAY),
        scheduled=[
            {
                "after_seconds": HOUR,
                "name": "rails.limit_change",
                "payload": {"handle": USER, "per_txn_paise": 1_000},
            }
        ],
    )
    state["disputes"] = {
        "inbox": [
            {"sender": "Bank", "subject": "Already here", "body": "seeded at start"},
            {
                "sender": "Bank",
                "subject": "Later",
                "body": "in two hours",
                "after_seconds": 2 * HOUR,
            },
        ]
    }
    session = Session(make_task(state))
    assert session.episode.clock.now() == EPOCH + dt.timedelta(days=10)
    assert session.admin("rails", "limits") == []
    assert [m["subject"] for m in session.admin("disputes", "messages")].count("Later") == 0
    waited = session.ok("wait", hours=3)
    assert [m["subject"] for m in waited["new_messages"]] == ["Later"]
    assert session.admin("rails", "limits")[0]["per_txn_paise"] == 1_000


def test_shifting_leaves_a_task_without_history_alone() -> None:
    state = base_state()
    assert shifted(state, 0) == state
    assert shifted(state, 0) is not state


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**kettle(DAY), "merchant_id": "nobody"}, "unknown merchant"),
        ({**kettle(DAY), "items": [{"product_id": "slow-goods-kettle"}]}, "does not sell"),
        ({"kind": "payment", "seconds_ago": DAY, "payee": USER, "amount_paise": 1}, "another"),
        ({"kind": "refund", "seconds_ago": DAY}, "kind"),
    ],
)
def test_history_is_checked_against_the_world(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PaymentsState.model_validate(with_history(entry))


def test_history_the_world_refuses_fails_loudly_at_load() -> None:
    state = with_history(
        {"kind": "payment", "seconds_ago": DAY, "payee": handle(HONEST), "amount_paise": 9_000_000}
    )
    with pytest.raises(ValueError, match="history entry 1: the payment was declined"):
        Session(make_task(state))
    out_of_stock = copy.deepcopy(with_history(kettle(DAY)))
    out_of_stock["commerce"]["products"][0]["stock"] = 0
    with pytest.raises(ValueError, match="history entry 1: add_to_cart refused: out_of_stock"):
        Session(make_task(out_of_stock))
