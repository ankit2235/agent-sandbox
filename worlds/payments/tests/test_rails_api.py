"""The rails through HTTP, as Phase 2's tools and the graders will reach them."""

from typing import Any

import pytest
from rails_testkit import ALICE, BOB, SHOP, config_dict

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.service import SIM_TIME_HEADER
from kit.world import WorldState
from worlds.payments.services.rails import RailsService


def service(config: dict[str, Any] | None = None) -> tuple[RailsService, SimClock]:
    clock = SimClock(EPOCH)
    rails = RailsService(clock, SeededRNG(7))
    rails.load(config if config is not None else config_dict())
    return rails, clock


def deliver(rails: RailsService, clock: SimClock, seconds: float) -> list[Any]:
    """What the Phase 2 episode will do: advance the clock and hand the rails their events."""
    clock.advance(seconds)
    return [rails.apply_event(event) for event in clock.due() if rails.handles(event)]


def payment(key: str = "k-1", amount: Any = 10_000, **extra: Any) -> dict[str, Any]:
    return {"idempotency_key": key, "payer": ALICE, "payee": SHOP, "amount_paise": amount, **extra}


def test_the_rails_are_a_kit_world_state() -> None:
    rails, _ = service()
    assert isinstance(rails, WorldState)
    assert rails.name == "rails"


def test_create_then_replay_then_conflict() -> None:
    rails, _ = service()
    created = rails.post("/payments", payment())
    assert created.status_code == 201
    replayed = rails.post("/payments", payment())
    assert replayed.status_code == 200
    assert replayed.json()["payment"]["id"] == created.json()["payment"]["id"]
    conflict = rails.post("/payments", payment(amount=10_001))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_a_decline_is_recorded_and_returned_as_data() -> None:
    rails, _ = service()
    response = rails.post("/payments", payment(amount=900_000))
    assert response.status_code == 201
    assert response.json()["payment"]["failure_code"] == "insufficient_funds"


@pytest.mark.parametrize(
    "body",
    [
        payment(amount=10.5),
        payment(amount="10000"),
        payment(payee="shop@okaxis"),
        {"payer": ALICE, "payee": SHOP, "amount_paise": 1},
        payment(key="has spaces"),
        payment(colour="blue"),
    ],
    ids=["float", "string-amount", "real-looking-handle", "no-key", "bad-key", "extra-field"],
)
def test_malformed_requests_are_refused_with_one_code(body: dict[str, Any]) -> None:
    rails, _ = service()
    response = rails.post("/payments", body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert rails.query("payments") == []


def test_a_call_without_simulated_time_fails_loudly() -> None:
    rails, _ = service()
    with pytest.raises(ValueError, match=SIM_TIME_HEADER):
        rails.request("POST", "/payments", json_body=payment(), headers={})


def test_a_timeout_schedules_its_own_settlement_on_the_episode_clock() -> None:
    config = config_dict(faults={"timeout_on_attempts": [1]})
    rails, clock = service(config)
    body = rails.post("/payments", payment(key="maybe")).json()
    assert body["timed_out"] is True
    assert [event.name for event in clock.pending] == ["rails.settle"]
    assert rails.get("/payments", idempotency_key="maybe")["status"] == "pending"

    [diff] = deliver(rails, clock, 90)
    assert diff[0].path == f"rails/payment/{body['payment']['id']}/status"
    assert (diff[0].before, diff[0].after) == ("pending", "success")
    assert rails.get("/payments", idempotency_key="maybe")["status"] == "success"


def test_lookups_and_their_not_found_codes() -> None:
    rails, _ = service()
    assert rails.get(f"/accounts/{BOB}")["balance_paise"] == 100_000
    assert rails.get("/handles/ghost@sandbox") == {
        "handle": "ghost@sandbox",
        "exists": False,
        "holder": None,
    }
    assert rails.get(f"/handles/{SHOP}")["holder"] == "Example Shop"
    assert rails.get(f"/limits/{ALICE}")["daily_window"] == "rolling_24h"
    for path, code in [
        ("/accounts/ghost@sandbox", "account_not_found"),
        ("/payments/pay_nope", "payment_not_found"),
        ("/payments?idempotency_key=never-sent", "payment_not_found"),
        ("/refunds/rfd_nope", "refund_not_found"),
        ("/mandates/nope", "mandate_not_found"),
        ("/admin/secrets", "unknown_resource"),
    ]:
        response = rails.request("GET", path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == code


def test_refunds_and_mandate_actions_over_http() -> None:
    mandate = {
        "id": "m1",
        "payer": ALICE,
        "payee": SHOP,
        "type": "recurring",
        "frequency": "monthly",
        "max_amount_paise": 10_000,
    }
    rails, _ = service(config_dict(mandates=[mandate]))
    paid = rails.post("/payments", payment()).json()["payment"]
    refund = rails.post(
        "/refunds",
        {
            "idempotency_key": "r-1",
            "payment_id": paid["id"],
            "amount_paise": 2_500,
            "reason": "damaged",
        },
    )
    assert refund.status_code == 201
    assert refund.json()["refund"]["status"] == "success"
    paused = rails.post("/mandates/m1/pause")
    assert paused.json()["mandate"]["status"] == "paused"
    assert rails.post("/mandates/m1/pause").json()["error"]["code"] == "invalid_transition"
    assert rails.post("/mandates/m1/explode").status_code == 422


def test_admin_reads_never_change_state() -> None:
    rails, _ = service()
    rails.post("/payments", payment())
    before = rails.snapshot()
    state = rails.query("state")
    assert set(state) == {"accounts", "mandates", "limits", "payments", "refunds", "ledger"}
    assert len(rails.query("payments", idempotency_key="k-1")) == 1
    rails.query("ledger")
    assert rails.snapshot() == before


def test_a_bad_initial_state_is_refused_with_the_reason() -> None:
    clock = SimClock(EPOCH)
    rails = RailsService(clock, SeededRNG(7))
    with pytest.raises(ValueError, match="unknown handles"):
        rails.load(config_dict(limits={"ghost@sandbox": {"daily_paise": 1}}))
