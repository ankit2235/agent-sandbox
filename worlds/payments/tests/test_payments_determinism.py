"""Same seed and same calls give byte-identical state in all three services; reset is exact."""

from typing import Any

from payments_testkit import Session, base_state, make_task

from kit.scripted import run_script
from kit.types import ToolCall
from worlds.payments import world_spec

MIXUP, VAGUE = "mixup-goods", "vague-goods"


def busy_state() -> dict[str, Any]:
    """Every service busy: seeded wrong items and timeouts, a subscription, an inbox."""
    state = base_state()
    state["rails"]["faults"] = {"timeout_rate": 0.3}
    state["rails"]["mandates"] = [{
        "id": "box-mandate", "payer": "asha@sandbox", "payee": "honest-goods@sandbox",
        "type": "recurring", "max_amount_paise": 30_000, "frequency": "monthly",
    }]  # fmt: skip
    state["commerce"]["subscriptions"] = [
        {"id": "box", "plan_id": "honest-basic", "mandate_id": "box-mandate",
         "next_billing_after_seconds": 7_200},
    ]  # fmt: skip
    state["disputes"] = {"inbox": [{"sender": "Sandbox Pay", "subject": "Hi", "body": "Hello",
                                    "after_seconds": 600}]}  # fmt: skip
    return state


def scripted_calls() -> list[ToolCall]:
    """A fixed agent: buys from two merchants, retries, refunds, disputes, waits."""
    calls: list[ToolCall] = []
    for merchant, n in ((MIXUP, 1), (VAGUE, 2), (MIXUP, 3)):
        calls += [
            ToolCall(tool="add_to_cart", args={"merchant_id": merchant,
                                               "product_id": f"{merchant}-kettle", "quantity": 1}),
            ToolCall(tool="checkout", args={"merchant_id": merchant}),
            ToolCall(tool="get_balance"),
        ]  # fmt: skip
        for key in (f"k{n}", f"k{n}-retry"):
            calls.append(ToolCall(tool="list_transactions", args={"limit": 5}))
            calls.append(ToolCall(tool="pay", args={
                "payee": f"{merchant}@sandbox", "amount_paise": 149_900, "idempotency_key": key,
            }))  # fmt: skip
    calls += [
        ToolCall(tool="send_message", args={"merchant_id": VAGUE, "body": "Hello?"}),
        ToolCall(tool="wait", args={"hours": 80}),
        ToolCall(tool="read_inbox", args={}),
        ToolCall(tool="create_mandate", args={"payee": "honest-goods@sandbox", "type": "reserve",
                                              "max_amount_paise": 50_000, "valid_days": 2}),
        ToolCall(tool="wait", args={"hours": 72}),
        ToolCall(tool="list_mandates", args={}),
    ]  # fmt: skip
    return calls


def run(seed: int) -> tuple[dict[str, bytes], str]:
    task = make_task(busy_state(), seed=seed)
    result = run_script(world_spec(), task, scripted_calls(), grade=False)
    lines = [step.model_dump_json() for step in result.trace.steps]
    return result.final_state, "\n".join(lines)


def test_same_seed_same_calls_give_identical_bytes_in_every_service() -> None:
    first_state, first_trace = run(11)
    second_state, second_trace = run(11)
    assert sorted(first_state) == ["commerce", "disputes", "rails"]
    assert first_state == second_state
    assert first_trace == second_trace


def test_a_different_seed_gives_a_different_run() -> None:
    assert run(11)[0] != run(12)[0]


def test_the_script_exercises_all_three_services() -> None:
    task = make_task(busy_state(), seed=11)
    session = Session(task)
    for call in scripted_calls():
        session.episode.step(call)
    paths = {d.path.split("/")[0] for step in session.episode.trace.steps for d in step.state_diff}
    assert paths == {"rails", "commerce", "disputes"}
    events = {step.event.split(".")[0] for step in session.episode.trace.events() if step.event}
    assert events == {"rails", "commerce", "disputes"}


def test_close_restores_all_three_services_exactly() -> None:
    session = Session(make_task(busy_state(), seed=11))
    baseline = session.episode.snapshot_all()
    for call in scripted_calls():
        session.episode.step(call)
    assert all(session.episode.snapshot_all()[name] != baseline[name] for name in baseline)
    session.episode.close()
    assert session.episode.snapshot_all() == baseline


def test_a_fresh_episode_after_reset_replays_identically() -> None:
    session = Session(make_task(busy_state(), seed=11))
    for call in scripted_calls():
        session.episode.step(call)
    finished = session.episode.snapshot_all()
    session.episode.close()
    again = Session(make_task(busy_state(), seed=11))
    for call in scripted_calls():
        again.episode.step(call)
    assert again.episode.snapshot_all() == finished


def test_admin_reads_change_nothing() -> None:
    session = Session(make_task(busy_state(), seed=11))
    for call in scripted_calls()[:8]:
        session.episode.step(call)
    before = session.episode.snapshot_all()
    for service in ("rails", "commerce", "disputes"):
        session.admin(service, "state")
    assert session.episode.snapshot_all() == before
