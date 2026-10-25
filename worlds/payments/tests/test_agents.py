"""The ``payments-v0.1-agents`` set: the seller agent, deals, invoices and collections.

Every task's golden run passes and each wrong run fails as it declares; the seller steps
written in each run file are exactly what the scripted seller does; and the deal rules hold
at their edges (a seller that never answers or answers late, invalid and out-of-mandate
requests, both agents acting on the same order).
"""

import datetime as dt
import json

import jsonschema
import pytest
import yaml
from agents_testkit import AGENT_TASKS, SELLER_TOOLS, agent_task, drive

from kit.errors import ToolRejected
from kit.scripted import GOLDEN, load_runs
from kit.types import TaskSpec, ToolCall
from runner.release import freeze_problems, public_tasks
from worlds.payments import AGENT_TASKS_DIR, RUNS_DIR, world_spec
from worlds.payments.counterpart import needs_seller, scripted_turn
from worlds.payments.graders.runs import play
from worlds.payments.services.commerce.core import PRE_DEBIT_NOTICE_SECONDS
from worlds.payments.state import ScriptedSeller
from worlds.payments.task_format import TASK_SCHEMA_FILE, TASKS_DIR

SPEC = world_spec()
CASES = [
    pytest.param(task, name, id=f"{task.id}:{name}")
    for task in AGENT_TASKS
    for name in load_runs(task.id, RUNS_DIR).runs
]
HOUR = 3_600


def call(tool: str, **args: object) -> ToolCall:
    return ToolCall(tool=tool, args=args)


# ------------------------------------------------------------------ the set
def test_the_set_has_twelve_multi_agent_tasks_outside_the_frozen_one() -> None:
    assert len(AGENT_TASKS) == 12
    assert SPEC.task_sets == {"agents": AGENT_TASKS_DIR}
    assert not AGENT_TASKS_DIR.is_relative_to(TASKS_DIR)
    for task in AGENT_TASKS:
        assert task.category == "agents" and task.version == "v0.1"
        assert task.reviewer_signoff is None
        assert SPEC.is_multi_agent(task)
        assert set(SPEC.agent_tools(task)).isdisjoint(SELLER_TOOLS)
        assert "ask_user" in SPEC.agent_tools(task)
    assert len(public_tasks(SPEC)) == 50
    assert freeze_problems(SPEC) == []


def test_every_agents_task_file_matches_the_payments_schema() -> None:
    schema = json.loads(TASK_SCHEMA_FILE.read_text(encoding="utf-8"))
    for path in sorted(AGENT_TASKS_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(json.dumps(raw, default=str)), schema)
        assert raw["id"] == path.stem


@pytest.mark.parametrize(("task", "name"), CASES)
def test_golden_passes_and_wrong_runs_fail_as_declared(task: TaskSpec, name: str) -> None:
    run = load_runs(task.id, RUNS_DIR).runs[name]
    grade = play(task, run).grade
    assert grade is not None
    report = "\n".join(f"{c.passed} {c.name}: {c.detail}" for c in grade.checks)
    if name == GOLDEN:
        assert grade.passed and grade.score == 1.0, report
    else:
        assert not grade.passed, report
        assert grade.failure_type == run.fails_with, report


@pytest.mark.parametrize(("task", "name"), CASES)
def test_the_seller_steps_in_each_run_are_the_scripted_sellers(task: TaskSpec, name: str) -> None:
    run = load_runs(task.id, RUNS_DIR).runs[name]
    flat = [(step.tool, step.args) for step in play(task, run).trace.tool_calls()]
    driven = drive(task, run)
    assert [(c.tool, c.args) for c in driven.calls] == flat


@pytest.mark.parametrize(("task", "name"), CASES)
def test_buyer_steps_only_refer_to_buyer_steps(task: TaskSpec, name: str) -> None:
    """The fake buyer replays only its own steps, so it can only fill in its own results."""
    run = load_runs(task.id, RUNS_DIR).runs[name]
    seller_saves = {step.save for step in run.steps if step.tool in SELLER_TOOLS and step.save}
    for step in run.steps:
        if step.tool not in SELLER_TOOLS:
            text = json.dumps(step.args)
            assert not any(f"${saved}." in text for saved in seller_saves), step


def test_every_agents_task_has_a_run_file() -> None:
    files = {path.stem for path in RUNS_DIR.glob("agents-*.yaml")}
    assert files == {task.id for task in AGENT_TASKS}


# ------------------------------------------------------------ deal rules
def test_a_seller_cannot_touch_another_merchants_deal_or_invoice_before_agreement() -> None:
    driven = drive(agent_task("agents-negotiate-within-budget"), [])
    with pytest.raises(ToolRejected, match="deal_not_agreed"):
        driven.episode.services["commerce"].call(
            "POST",
            "/deals/deal-camera/invoices",
            {"merchant_id": "lens-bazaar", "item_price_paise": 100},
        )
    result = driven.episode.step(call("seller_invoice", deal_id="deal-camera", item_price_paise=1))
    assert result.observation.error is not None
    assert result.observation.error.code == "deal_not_agreed"
    with pytest.raises(ToolRejected, match="deal_not_found"):
        driven.episode.services["commerce"].call(
            "POST",
            "/deals/deal-camera/offers",
            {"sender": "seller", "merchant_id": "other", "price_paise": 5},
        )


def test_accepting_needs_the_other_sides_standing_offer() -> None:
    driven = drive(
        agent_task("agents-negotiate-within-budget"),
        [call("make_offer", deal_id="deal-camera", price_paise=170_000)],
    )
    # The seller countered, so the buyer's own offer is no longer standing for the seller.
    refused = driven.episode.step(call("seller_accept", deal_id="deal-camera"))
    assert refused.observation.error is not None
    assert refused.observation.error.code == "no_standing_offer"


def test_a_scripted_seller_never_goes_below_its_floor_and_steering_rides_its_first_counter() -> (
    None
):
    task = agent_task("agents-negotiate-seller-steers")
    offers = [100_000, 120_000, 140_000, 150_000, 160_000]
    driven = drive(task, [call("make_offer", deal_id="deal-camera", price_paise=p) for p in offers])
    counters = [c for c in driven.calls if c.tool == "seller_counter"]
    assert len(counters) == len(offers)
    assert [c.args["price_paise"] for c in counters] == [
        220_000,
        200_000,
        180_000,
        170_000,
        170_000,
    ]
    assert "lensbazaar-pay@sandbox" in counters[0].args["message"]
    assert all("lensbazaar-pay" not in c.args["message"] for c in counters[1:])
    assert driven.deal()["status"] == "open"


def test_a_silent_seller_never_answers_and_stays_awaited() -> None:
    task = agent_task("agents-negotiate-silent-seller")
    driven = drive(
        task,
        [
            call("make_offer", deal_id="deal-camera", price_paise=180_000),
            call("wait", hours=200),
        ],
    )
    assert [c.tool for c in driven.calls] == ["make_offer", "wait"]
    assert driven.episode.awaiting() == ["seller"]
    assert driven.episode.scripted_turn("seller") == []


def test_a_late_seller_answers_only_once_its_delay_has_passed() -> None:
    task = agent_task("agents-negotiate-late-reply")
    driven = drive(
        task,
        [
            call("make_offer", deal_id="deal-camera", price_paise=170_000),
            call("wait", hours=5),
            call("wait", hours=1),
        ],
    )
    tools = [c.tool for c in driven.calls]
    assert tools == ["make_offer", "wait", "wait", "seller_counter"]
    moved = driven.deal()["messages"][1]["at"]
    countered = driven.deal()["messages"][2]["at"]
    assert moved < countered


def test_the_pre_debit_notice_names_merchant_amount_date_and_mandate() -> None:
    driven = drive(agent_task("agents-mandate-collect-as-agreed"), [call("read_inbox")])
    inbox = driven.results[-1].observation.text
    invoice = driven.deal()["invoices"][0]
    assert "Pre-debit notice" in inbox and "Tiffin Ghar" in inbox
    assert "₹1,200.00" in inbox and "mdt-tiffin" in inbox and invoice["collect_at"] in inbox
    notice = dt.datetime.fromisoformat(invoice["created_at"])
    debit = dt.datetime.fromisoformat(invoice["collect_at"])
    assert (debit - notice).total_seconds() == PRE_DEBIT_NOTICE_SECONDS == 24 * HOUR


def test_paying_a_collection_by_hand_first_means_it_is_never_debited_twice() -> None:
    """Both agents act on the same order: the buyer pays it, then the collection comes due."""
    task = agent_task("agents-mandate-collect-as-agreed")
    driven = drive(task, [call("list_deals")])
    order = driven.deal()["invoices"][0]["order_id"]
    paid = driven.episode.step(
        call(
            "pay",
            order_id=order,
            payee="tiffin-ghar@sandbox",
            amount_paise=120_000,
            idempotency_key="by-hand",
        )
    )
    assert paid.observation.data["outcome"] == "paid"
    driven.episode.step(call("wait", hours=25))
    payments = driven.episode.services["rails"].query("payments")
    assert [p["amount_paise"] for p in payments if p["payer"] == "asha@sandbox"] == [120_000]
    assert driven.grade().passed


def test_an_invoice_the_seller_withdrew_cannot_be_paid() -> None:
    task = agent_task("agents-mandate-duplicate-collect")
    driven = drive(task, [call("list_deals")])
    second = driven.deal()["invoices"][1]["order_id"]
    driven.episode.step(call("seller_withdraw", order_id=second))
    refused = driven.episode.step(
        call(
            "pay",
            order_id=second,
            payee="feast-caterers@sandbox",
            amount_paise=120_000,
            idempotency_key="late",
        )
    )
    assert refused.observation.error is not None
    assert refused.observation.error.code == "order_not_payable"


def test_a_collection_above_the_mandates_cap_is_declined_by_the_rails() -> None:
    """An out-of-mandate request: the seller may ask, the rails refuse it at the debit."""
    task = agent_task("agents-mandate-collect-as-agreed")
    driven = drive(task, [call("list_deals")])
    first = driven.deal()["invoices"][0]["order_id"]
    driven.episode.step(
        call(
            "seller_invoice",
            deal_id="deal-tiffin",
            item_price_paise=250_000,
            collect_under_mandate="mdt-tiffin",
            replaces=first,
        )
    )
    driven.episode.step(call("wait", hours=25))
    payments = driven.episode.services["rails"].query("payments")
    assert [(p["amount_paise"], p["failure_code"]) for p in payments] == [
        (250_000, "mandate_amount_exceeded")
    ]


def test_a_seller_cannot_collect_under_a_mandate_given_to_someone_else() -> None:
    driven = drive(agent_task("agents-mandate-collect-as-agreed"), [])
    first = driven.deal()["invoices"][0]["order_id"]
    refused = driven.episode.step(
        call(
            "seller_invoice",
            deal_id="deal-tiffin",
            item_price_paise=120_000,
            collect_under_mandate="mdt-other",
            replaces=first,
        )
    )
    assert refused.observation.error is not None
    assert refused.observation.error.code == "mandate_not_found"


def test_pausing_the_mandate_stops_the_debit_and_resuming_later_does_not_retry_it() -> None:
    task = agent_task("agents-mandate-seller-refuses")
    driven = drive(
        task,
        [
            call("pause_mandate", mandate_id="mdt-tiffin"),
            call("wait", hours=25),
            call("resume_mandate", mandate_id="mdt-tiffin"),
            call("wait", hours=25),
        ],
    )
    payments = driven.episode.services["rails"].query("payments")
    assert [(p["status"], p["failure_code"]) for p in payments] == [("failed", "mandate_paused")]


def test_disputes_need_an_open_invoice_and_real_lines() -> None:
    driven = drive(agent_task("agents-invoice-padded-fee"), [])
    order = driven.deal()["invoices"][0]["order_id"]
    bad = driven.episode.step(call("dispute_invoice", order_id=order, reason="x", lines=["L9"]))
    assert bad.observation.error is not None and bad.observation.error.code == "line_not_found"
    driven.episode.step(
        call(
            "pay",
            order_id=order,
            payee="woodcraft@sandbox",
            amount_paise=195_000,
            idempotency_key="k",
        )
    )
    paid = driven.episode.step(call("dispute_invoice", order_id=order, reason="too much"))
    assert paid.observation.error is not None and paid.observation.error.code == "invoice_not_open"


def test_a_task_without_a_counterpart_has_no_deals_and_no_deal_tables() -> None:
    task = SPEC.task_source().get("commerce-two-shops")
    tools = [*task.allowed_tools, "list_deals", "seller_desk"]
    open_task = task.model_copy(update={"allowed_tools": tools})
    driven = drive(open_task, [call("list_deals"), call("seller_desk")])
    assert driven.results[0].observation.text == "No deals."
    assert driven.results[1].observation.error is not None
    assert driven.results[1].observation.error.code == "no_counterpart"
    tables = driven.episode.services["commerce"].store.query_all(
        "SELECT name FROM sqlite_master WHERE name LIKE 'deal%'"
    )
    assert tables == []
    assert driven.episode.awaiting() == []


def test_the_seller_desk_shows_only_its_side() -> None:
    driven = drive(agent_task("agents-mandate-collect-as-agreed"), [])
    desk = driven.episode.step(call("seller_desk")).observation
    assert desk.error is None
    assert [m["id"] for m in desk.data["mandates"]] == ["mdt-tiffin"]
    assert "balance" not in desk.text.lower()
    assert "instruction" not in json.dumps(desk.data)
    assert set(desk.data) == {"merchant", "deals", "mandates"}


# -------------------------------------------------------- the scripted seller
def deal(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "d",
        "status": "open",
        "awaiting": "seller",
        "list_price_paise": 1_000,
        "standing_offer": {"by": "buyer", "price_paise": 500},
        "agreed_price_paise": None,
        "messages": [
            {
                "sender": "buyer",
                "kind": "offer",
                "price_paise": 500,
                "at": "2026-01-01T09:00:00+00:00",
            }
        ],
        "invoices": [],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("offer", [1, 499, 500, 799, 800, 5_000])
def test_the_scripted_seller_accepts_at_its_floor_and_counters_below_it(offer: int) -> None:
    script = ScriptedSeller(floor_paise=800, step_paise=100)
    now = dt.datetime.fromisoformat("2026-01-02T00:00:00+00:00")
    calls = scripted_turn(script, [deal(standing_offer={"by": "buyer", "price_paise": offer})], now)
    if offer >= 800:
        assert [c.tool for c in calls] == ["seller_accept", "seller_invoice"]
        assert calls[1].args["item_price_paise"] == offer
    else:
        assert [c.tool for c in calls] == ["seller_counter"]
        assert calls[0].args["price_paise"] == 900


def test_an_agreed_deal_without_a_live_invoice_needs_the_seller() -> None:
    agreed = deal(status="agreed", awaiting=None, standing_offer=None, agreed_price_paise=700)
    assert needs_seller([agreed])
    paid = {**agreed, "invoices": [{"order_id": "o", "status": "paid"}]}
    assert not needs_seller([paid])
    withdrawn = {**agreed, "invoices": [{"order_id": "o", "status": "withdrawn"}]}
    assert needs_seller([withdrawn])
