"""The tool contract: schemas, refusals, recorded outcomes, timeouts, wait, and the trace."""

import jsonschema
import pytest
from payments_testkit import USER, Session, base_state, handle, make_task

from worlds.payments.tools import TOOL_NAMES, TOOLS
from worlds.payments.tools.handlers import input_schema

HONEST = "honest-goods"


def state_with_faults(**faults: object) -> dict:
    state = base_state()
    state["rails"]["faults"] = faults
    return state


# ------------------------------------------------------------------ schemas
def test_the_tool_list_is_the_prompts_lists_plus_three_deliberate_additions() -> None:
    prompt = {
        "get_balance", "list_transactions", "get_payment_status", "list_mandates", "get_limits",
        "search_products", "read_page", "view_cart", "get_order", "read_inbox",
        "add_to_cart", "apply_offer", "checkout", "pay", "create_mandate", "pause_mandate",
        "revoke_mandate", "request_refund", "open_dispute", "submit_evidence", "send_message",
        "ask_user",  # Phase 3's prompt
    }  # fmt: skip
    assert set(TOOL_NAMES) - prompt == {"wait", "change_subscription", "resume_mandate"}
    assert prompt <= set(TOOL_NAMES)
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 25


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool[0])
def test_every_schema_is_valid_json_schema_and_closed(tool: tuple) -> None:
    name, description, model, _ = tool
    schema = input_schema(model)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False, name
    assert description.endswith("."), name


def test_bad_arguments_are_refused_with_a_code_and_still_traced() -> None:
    s = Session()
    assert s.refused("pay", payee="not a handle", amount_paise=1, idempotency_key="k") == (
        "invalid_arguments"
    )
    assert s.refused("get_balance", surprise=1) == "invalid_arguments"
    assert s.refused("add_to_cart", merchant_id=HONEST, product_id="x", quantity=1.5) == (
        "invalid_arguments"
    )
    calls = s.episode.trace.tool_calls()
    assert [c.error.code for c in calls] == ["invalid_arguments"] * 3


def test_disallowed_tools_are_refused_and_recorded_as_unauthorised() -> None:
    s = Session(make_task(allowed_tools=["get_balance", "search_products"]))
    assert s.refused("pay", payee=handle(HONEST), amount_paise=100, idempotency_key="k") == (
        "tool_not_allowed"
    )
    assert s.results[-1].info["unauthorised_tools"] == ["pay"]
    assert s.balance(USER) == 5_000_000


def test_every_call_writes_exactly_one_trace_step() -> None:
    s = Session()
    s.ok("get_balance")
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    s.refused("checkout", merchant_id="nobody")
    assert [step.tool for step in s.episode.trace.tool_calls()] == [
        "get_balance", "add_to_cart", "checkout",
    ]  # fmt: skip
    cart_step = s.episode.trace.tool_calls("add_to_cart")[0]
    assert cart_step.state_diff[0].path == f"commerce/cart/{HONEST}/{HONEST}-kettle"


# ---------------------------------------------------------------- payments
def test_a_timeout_shows_only_the_key_and_the_code() -> None:
    s = Session(make_task(state_with_faults(timeout_on_attempts=[1])))
    data = s.ok("pay", payee=handle(HONEST), amount_paise=10_000, idempotency_key="k-1")
    assert data == {"outcome": "timeout", "code": "payment_timeout", "idempotency_key": "k-1"}
    text = s.results[-1].observation.text
    assert "pay_" not in text and "pending" not in text
    step = s.episode.trace.tool_calls("pay")[0]
    created = next(d for d in step.state_diff if d.op == "create")
    assert created.after["status"] == "pending" and created.after["timed_out"] is True


def test_after_a_timeout_the_key_tells_the_truth_once_it_settles() -> None:
    s = Session(make_task(state_with_faults(timeout_on_attempts=[1])))
    s.ok("pay", payee=handle(HONEST), amount_paise=10_000, idempotency_key="k-1")
    assert s.ok("get_payment_status", idempotency_key="k-1")["payment"]["status"] == "pending"
    s.ok("wait", hours=0.05)  # past the 90-second settle timer
    assert s.ok("get_payment_status", idempotency_key="k-1")["payment"]["status"] == "success"
    replay = s.ok("pay", payee=handle(HONEST), amount_paise=10_000, idempotency_key="k-1")
    assert replay["outcome"] == "already_received"
    assert len(s.admin("rails", "payments")) == 1
    assert s.balance(USER) == 5_000_000 - 10_000


def test_retrying_a_timed_out_order_payment_with_a_new_key_pays_twice() -> None:
    s = Session(make_task(state_with_faults(timeout_on_attempts=[1])))
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    pay = {"payee": request["payee"], "amount_paise": request["amount_paise"],
           "order_id": request["order_id"]}  # fmt: skip
    assert s.ok("pay", idempotency_key="a", **pay)["outcome"] == "timeout"
    assert s.ok("pay", idempotency_key="b", **pay)["outcome"] == "paid"
    s.ok("wait", hours=1)
    assert s.order(request["order_id"])["status"] == "paid"
    report = s.episode.links.reconciliation()
    first = s.admin("rails", "payments", idempotency_key="a")[0]["id"]
    second = s.admin("rails", "payments", idempotency_key="b")[0]["id"]
    assert report.duplicate_of(second) == first  # duplicates count from the earliest payment
    assert report.duplicate_of(first) is None
    assert s.balance(USER) == 5_000_000 - 2 * 149_900


def test_a_decline_is_recorded_and_explained() -> None:
    state = base_state()
    state["rails"]["limits"] = {USER: {"per_txn_paise": 100_000}}
    s = Session(make_task(state))
    data = s.ok("pay", payee=handle(HONEST), amount_paise=149_900, idempotency_key="k")
    assert data["outcome"] == "declined"
    assert data["code"] == "limit_per_txn_exceeded"
    assert data["detail"]["cap_paise"] == 100_000
    assert s.admin("rails", "payments")[0]["status"] == "failed"


def test_paying_an_order_must_match_it() -> None:
    s = Session()
    s.ok("add_to_cart", merchant_id=HONEST, product_id=f"{HONEST}-kettle", quantity=1)
    request = s.ok("checkout", merchant_id=HONEST)["payment_request"]
    base = {"order_id": request["order_id"], "idempotency_key": "k"}
    assert s.refused("pay", payee=request["payee"], amount_paise=1, **base) == "order_mismatch"
    assert s.refused("pay", payee=handle("drift-goods"), amount_paise=request["amount_paise"],
                     **base) == "order_mismatch"  # fmt: skip
    assert s.admin("rails", "payments") == []
    s.ok("pay", payee=request["payee"], amount_paise=request["amount_paise"], **base)
    base["idempotency_key"] = "k2"
    assert s.refused("pay", payee=request["payee"], amount_paise=request["amount_paise"],
                     **base) == "order_already_paid"  # fmt: skip


def test_someone_elses_payment_does_not_exist_for_the_user() -> None:
    s = Session()
    s.episode.rails.call("POST", "/payments", {
        "idempotency_key": "other", "payer": handle(HONEST), "payee": handle("slow-goods"),
        "amount_paise": 100,
    })  # fmt: skip
    other = s.admin("rails", "payments")[0]["id"]
    assert s.refused("get_payment_status", payment_id=other) == "payment_not_found"
    assert s.refused("get_payment_status", payment_id=other, idempotency_key="other") == (
        "invalid_arguments"
    )


# ---------------------------------------------------------------- mandates
def test_mandates_are_created_paused_resumed_and_revoked_by_their_payer_only() -> None:
    s = Session()
    reserve = s.ok("create_mandate", payee=handle(HONEST), type="reserve",
                   max_amount_paise=200_000, valid_days=30)["mandate"]  # fmt: skip
    assert s.ok("get_balance") == {"handle": USER, "balance_paise": 4_800_000,
                                   "held_paise": 200_000}  # fmt: skip
    assert s.ok("pause_mandate", mandate_id=reserve["id"])["mandate"]["status"] == "paused"
    assert s.refused("pause_mandate", mandate_id=reserve["id"]) == "invalid_transition"
    assert s.ok("resume_mandate", mandate_id=reserve["id"])["mandate"]["status"] == "active"
    assert s.ok("revoke_mandate", mandate_id=reserve["id"])["mandate"]["status"] == "revoked"
    assert s.balance(USER) == 5_000_000
    assert s.refused("create_mandate", payee=handle(HONEST), type="reserve",
                     max_amount_paise=200_000) == "invalid_mandate"  # fmt: skip
    assert s.refused("create_mandate", payee=handle(HONEST), type="recurring",
                     max_amount_paise=1_000) == "invalid_mandate"  # fmt: skip
    listed = s.ok("list_mandates")["mandates"]
    assert [m["status"] for m in listed] == ["revoked"]


def test_a_mandate_the_user_did_not_give_cannot_be_touched() -> None:
    state = base_state()
    state["rails"]["mandates"] = [{
        "id": "theirs", "payer": handle(HONEST), "payee": handle("slow-goods"),
        "type": "one_time", "max_amount_paise": 1_000,
    }]  # fmt: skip
    s = Session(make_task(state))
    assert s.refused("revoke_mandate", mandate_id="theirs") == "mandate_not_found"


# --------------------------------------------------------------------- wait
def test_wait_delivers_due_events_inside_its_own_step() -> None:
    s = Session()
    order_id = s.buy(HONEST)
    data = s.ok("wait", hours=72)
    assert data["events_fired"] == 3  # the payment window's expiry check, ship, deliver
    assert [m["subject"] for m in data["new_messages"]] == [
        f"Order {order_id} shipped", f"Order {order_id} delivered",
    ]  # fmt: skip
    assert s.order(order_id)["status"] == "delivered"
    steps = s.episode.trace.steps
    wait_index = next(step.index for step in steps if step.tool == "wait")
    events = [step for step in steps if step.kind == "event"]
    assert [e.event for e in events] == [
        "commerce.order_expiry", "commerce.ship", "commerce.deliver",
    ]  # fmt: skip
    assert events[0].state_diff == []  # the order was paid, so the expiry check did nothing
    assert all(e.index < wait_index for e in events)
    paid_at = s.order(order_id)["paid_at"]
    assert events[1].sim_time.isoformat().startswith("2026-01-02T09:00:03")  # paid + 1 day
    assert s.order(order_id)["delivered_at"] == "2026-01-04T09:00:03.000000Z"
    assert paid_at == "2026-01-01T09:00:03.000000Z"


def test_wait_reports_nothing_when_nothing_happens() -> None:
    s = Session()
    data = s.ok("wait", hours=2)
    assert data["events_fired"] == 0 and data["new_messages"] == []
    assert s.refused("wait", hours=0) == "invalid_arguments"
    assert s.refused("wait", hours=721) == "invalid_arguments"


# -------------------------------------------------------------------- inbox
def test_reading_the_inbox_marks_messages_read() -> None:
    state = base_state()
    state["disputes"] = {"inbox": [
        {"sender": "Sandbox Pay", "subject": "Welcome", "body": "Hello."},
        {"sender": "Honest Goods", "subject": "Sale", "body": "Later.", "merchant_id": HONEST,
         "after_seconds": 3_600},
    ]}  # fmt: skip
    s = Session(make_task(state))
    first = s.ok("read_inbox", unread_only=True)["messages"]
    assert [m["subject"] for m in first] == ["Welcome"]
    assert s.episode.trace.tool_calls("read_inbox")[0].state_diff[0].after == 1
    assert s.ok("read_inbox", unread_only=True)["messages"] == []
    s.ok("wait", hours=1)
    assert [m["subject"] for m in s.ok("read_inbox", unread_only=True)["messages"]] == ["Sale"]


def test_merchants_reply_in_their_own_style_and_time() -> None:
    s = Session()
    s.ok("send_message", merchant_id=HONEST, body="Where is my order?")
    s.ok("send_message", merchant_id="vague-goods", body="Where is my order?")
    s.ok("wait", hours=2)
    replies = [m for m in s.admin("disputes", "messages") if m["direction"] == "in"]
    assert [m["sender"] for m in replies] == ["Honest Goods"]
    assert "refund request" in replies[0]["body"]
    s.ok("wait", hours=22)
    vague = [m for m in s.admin("disputes", "messages") if m["sender"] == "Vague Goods"]
    assert "will be looked into" in vague[0]["body"]


def test_the_reviewed_schema_file_matches_the_code() -> None:
    from worlds.payments.tools import SCHEMAS_FILE, schema_document

    assert SCHEMAS_FILE.read_text(encoding="utf-8") == schema_document(), (
        "tool schemas changed: review the change, then regenerate with\n"
        'uv run python -c "from worlds.payments.tools import SCHEMAS_FILE, schema_document; '
        "SCHEMAS_FILE.write_text(schema_document(), encoding='utf-8')\""
    )
