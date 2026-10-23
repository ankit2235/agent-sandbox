"""Same seed and same calls give byte-identical state; snapshots are exact and fast."""

import statistics
import time

from rails_testkit import ALICE, BOB, SHOP, Harness, config_dict

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from worlds.payments.services.rails import RailsService

SNAPSHOT_BUDGET_SECONDS = 0.050


def scripted_run(seed: int) -> bytes:
    """A small episode through HTTP, with a timeout, a refund, a block and a limit change."""
    clock = SimClock(EPOCH)
    rails = RailsService(clock, SeededRNG(seed))
    rails.load(
        config_dict(
            mandates=[
                {
                    "id": "block",
                    "payer": BOB,
                    "payee": SHOP,
                    "type": "reserve",
                    "max_amount_paise": 50_000,
                    "end_offset_seconds": 7_200,
                }
            ],
            faults={"timeout_on_attempts": [2], "timeout_rate": 0.25},
            scheduled=[
                {
                    "after_seconds": 600,
                    "name": "rails.limit_change",
                    "payload": {"handle": ALICE, "daily_paise": 40_000},
                }
            ],
        )
    )
    for index in range(12):
        clock.advance(120)
        for event in clock.due():
            rails.apply_event(event)
        response = rails.post(
            "/payments",
            {
                "idempotency_key": f"k-{index}",
                "payer": ALICE,
                "payee": SHOP,
                "amount_paise": 3_000 + index,
            },
        )
        paid = response.json()["payment"]
        if index % 4 == 0 and paid["status"] == "success":
            rails.post(
                "/refunds",
                {
                    "idempotency_key": f"r-{index}",
                    "payment_id": paid["id"],
                    "amount_paise": 1_000,
                    "reason": "partial",
                },
            )
    rails.post(
        "/payments",
        {
            "idempotency_key": "block-1",
            "payer": BOB,
            "payee": SHOP,
            "amount_paise": 20_000,
            "mandate_id": "block",
        },
    )
    clock.advance(86_400)
    for event in clock.due():
        rails.apply_event(event)
    return rails.snapshot()


def test_same_seed_same_calls_give_identical_database_bytes() -> None:
    assert scripted_run(7) == scripted_run(7)


def test_a_different_seed_gives_a_different_run() -> None:
    assert scripted_run(7) != scripted_run(8)


def test_restore_is_exact() -> None:
    h = Harness()
    h.pay(10_000)
    blob = h.rails.store.snapshot()
    h.pay(20_000)
    h.refund(h.admin("payments")[0]["id"], 5_000)
    h.rails.store.restore(blob)
    assert h.rails.store.snapshot() == blob
    assert len(h.admin("payments")) == 1
    assert h.pay(5_000).body["payment"]["status"] == "success"  # still usable after restore


def test_snapshot_and_restore_stay_under_50_ms_with_a_busy_episode() -> None:
    accounts = [
        {"handle": f"user{n}@sandbox", "holder": f"User {n}", "balance_paise": 1_000_000}
        for n in range(50)
    ]
    h = Harness(config_dict(accounts=[*config_dict()["accounts"], *accounts]))
    for n in range(2_000):
        h.pay(100 + n % 50, payer=f"user{n % 50}@sandbox", payee=SHOP)
    assert len(h.admin("payments")) == 2_000

    snapshots, restores = [], []
    for _ in range(5):
        start = time.perf_counter()
        blob = h.rails.store.snapshot()
        snapshots.append(time.perf_counter() - start)
        start = time.perf_counter()
        h.rails.store.restore(blob)
        restores.append(time.perf_counter() - start)
    assert statistics.median(snapshots) < SNAPSHOT_BUDGET_SECONDS
    assert statistics.median(restores) < SNAPSHOT_BUDGET_SECONDS
