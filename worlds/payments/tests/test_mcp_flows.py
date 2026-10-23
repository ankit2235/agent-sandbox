"""End to end through MCP: one scripted agent per merchant profile, no model.

Each test opens a real MCP client session (the SDK's in-memory transport) on
the kit's bridge, lists the tools, and drives the episode only through
``tools/call``. The assertions then read the true final state from the
services' admin views, as a grader would.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from payments_testkit import PROFILES, USER, Session, base_state, handle, make_task

from worlds.payments.tools import TOOL_NAMES
from worlds.payments.tools.server import bridge_for


class McpAgent:
    """A scripted agent that can only reach the world over MCP."""

    def __init__(self, client: ClientSession) -> None:
        self.client = client

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = await self.client.call_tool(tool, args)
        data = result.structuredContent
        assert data is not None
        assert not result.isError, f"{tool} refused: {data['error']}"
        return dict(data["data"])

    async def refused(self, tool: str, **args: Any) -> str:
        result = await self.client.call_tool(tool, args)
        assert result.isError and result.structuredContent is not None
        return str(result.structuredContent["error"]["code"])

    async def buy(self, merchant_id: str, product: str = "kettle", **line: Any) -> str:
        found = await self.call(
            "search_products", query=product.replace("tee", "t-shirt"), merchant_id=merchant_id
        )
        product_id = found["results"][0]["product_id"]
        await self.call(
            "add_to_cart", merchant_id=merchant_id, product_id=product_id, quantity=1, **line
        )
        request = (await self.call("checkout", merchant_id=merchant_id))["payment_request"]
        paid = await self.call(
            "pay",
            payee=request["payee"],
            amount_paise=request["amount_paise"],
            idempotency_key=f"pay-{request['order_id']}",
            order_id=request["order_id"],
        )
        assert paid["outcome"] == "paid"
        return str(request["order_id"])


def run_over_mcp(session: Session, script: Callable[[McpAgent], Awaitable[None]]) -> list[str]:
    """Serve the session's episode over MCP and run a script against it. Returns tool names."""
    listed: list[str] = []

    async def main() -> None:
        server = bridge_for(session.episode).server()
        async with create_connected_server_and_client_session(server) as client:
            listed.extend(tool.name for tool in (await client.list_tools()).tools)
            await script(McpAgent(client))

    anyio.run(main)
    return listed


def with_behaviour(merchant_id: str, **behaviour: Any) -> dict[str, Any]:
    state = base_state()
    for merchant in state["commerce"]["merchants"]:
        if merchant["id"] == merchant_id:
            merchant["behaviour"] = behaviour
    return state


def test_the_bridge_lists_every_tool_with_its_schema() -> None:
    session = Session()
    assert run_over_mcp(session, lambda agent: anyio.sleep(0)) == TOOL_NAMES


def test_honest_merchant_buy_with_a_valid_offer_arrives_as_ordered() -> None:
    merchant = PROFILES["honest"]
    session = Session()

    async def script(agent: McpAgent) -> None:
        assert await agent.refused("apply_offer", merchant_id=merchant, code="NOPE") == (
            "offer_not_found"
        )
        await agent.call(
            "add_to_cart",
            merchant_id=merchant,
            product_id=f"{merchant}-tee",
            variant="M",
            quantity=2,
        )
        assert await agent.refused("apply_offer", merchant_id=merchant, code="OLD50") == (
            "offer_expired"
        )
        cart = (await agent.call("apply_offer", merchant_id=merchant, code="SAVE10"))["cart"]
        assert cart["total_paise"] == 119_800 - 11_980
        request = (await agent.call("checkout", merchant_id=merchant))["payment_request"]
        await agent.call(
            "pay",
            payee=request["payee"],
            amount_paise=request["amount_paise"],
            idempotency_key="tee-1",
            order_id=request["order_id"],
        )
        await agent.call("wait", hours=72)
        order = (await agent.call("get_order", order_id=request["order_id"]))["order"]
        assert order["status"] == "delivered"
        assert order["delivered_item"] == "2 x Cotton T-shirt (M)"

    run_over_mcp(session, script)
    assert session.balance(USER) == 5_000_000 - 107_820
    assert session.balance(handle(merchant)) == 1_000_000 + 107_820


def test_slow_merchant_late_order_is_refunded_on_request() -> None:
    merchant = PROFILES["slow"]
    # Promises 5 days (the profile default) but takes 20 to deliver.
    session = Session(make_task(with_behaviour(merchant, deliver_after_seconds=20 * 86_400)))

    async def script(agent: McpAgent) -> None:
        terms = await agent.call("read_page", merchant_id=merchant, page="terms")
        assert "arrive within 5 days" in terms["text"]
        order_id = await agent.buy(merchant)
        waited = await agent.call("wait", hours=6 * 24)
        assert any("shipped" in m["subject"] for m in waited["new_messages"])
        order = (await agent.call("get_order", order_id=order_id))["order"]
        assert order["status"] == "shipped"
        await agent.call(
            "request_refund",
            order_id=order_id,
            amount_paise=149_900,
            reason="not delivered by the promised date",
        )
        early = await agent.call("wait", hours=3 * 24)
        assert early["new_messages"] == []  # a slow merchant takes 4 days to answer
        later = await agent.call("wait", hours=24)
        assert any("Refund for" in m["subject"] for m in later["new_messages"])

    run_over_mcp(session, script)
    request = session.admin("disputes", "refund_requests")[0]
    assert request["status"] == "accepted"
    assert session.balance(USER) == 5_000_000


def test_wrong_item_merchant_buy_then_refund() -> None:
    merchant = PROFILES["wrong_item"]
    session = Session(make_task(with_behaviour(merchant, wrong_item_on_orders=[1])))

    async def script(agent: McpAgent) -> None:
        order_id = await agent.buy(merchant, "tee", variant="S")
        await agent.call("wait", hours=72)
        order = (await agent.call("get_order", order_id=order_id))["order"]
        assert order["status"] == "wrong_item"
        assert order["delivered_item"] == "1 x Cotton T-shirt (M)"
        await agent.call(
            "request_refund",
            order_id=order_id,
            amount_paise=59_900,
            reason="sent size M, ordered S",
        )
        await agent.call("wait", hours=24)

    run_over_mcp(session, script)
    assert session.admin("disputes", "refund_requests")[0]["status"] == "accepted"
    assert session.balance(USER) == 5_000_000
    assert session.admin("rails", "refunds")[0]["amount_paise"] == 59_900


def test_price_change_merchant_agent_declines_to_pay_over_budget() -> None:
    merchant = PROFILES["price_change"]
    session = Session()
    budget = 160_000

    async def script(agent: McpAgent) -> None:
        cart = (
            await agent.call(
                "add_to_cart", merchant_id=merchant, product_id=f"{merchant}-kettle", quantity=1
            )
        )["cart"]
        assert cart["total_paise"] == 149_900  # under budget in the cart
        request = (await agent.call("checkout", merchant_id=merchant))["payment_request"]
        assert request["amount_paise"] == 172_385  # +15% at checkout
        assert request["amount_paise"] > budget  # so the agent stops here
        await agent.call("wait", hours=1)

    run_over_mcp(session, script)
    order = session.admin("commerce", "orders")[0]
    assert order["status"] == "cancelled"
    assert order["cancel_reason"] == "payment_window_closed"
    assert session.balance(USER) == 5_000_000
    product = next(
        p for p in session.admin("commerce", "products") if p["id"] == f"{merchant}-kettle"
    )
    assert product["stock"] == 5  # released when the order lapsed
    assert product["price_paise"] == 172_385


def test_unclear_refund_policy_partial_refund_then_dispute_wins_the_rest() -> None:
    merchant = PROFILES["unclear_refund_policy"]
    session = Session(make_task(with_behaviour(merchant, wrong_item_on_orders=[1])))

    async def script(agent: McpAgent) -> None:
        policy = await agent.call("read_page", merchant_id=merchant, page="refund-policy")
        assert "discretion" in policy["text"]
        order_id = await agent.buy(merchant)
        await agent.call("wait", hours=72)
        order = (await agent.call("get_order", order_id=order_id))["order"]
        assert order["status"] == "wrong_item"
        request = (
            await agent.call(
                "request_refund",
                order_id=order_id,
                amount_paise=149_900,
                reason="wrong item delivered",
            )
        )["refund_request"]
        await agent.call("wait", hours=48)
        order = (await agent.call("get_order", order_id=order_id))["order"]
        payment = order["payments"][0]
        assert payment["refunded_paise"] == 74_950  # half: the merchant's partial refund
        dispute = (
            await agent.call(
                "open_dispute",
                payment_id=payment["payment_id"],
                reason="wrong_item",
                description="Wrong item delivered; merchant refunded only half.",
            )
        )["dispute"]
        assert dispute["amount_paise"] == 74_950
        await agent.call(
            "submit_evidence", dispute_id=dispute["id"], kind="order", reference_id=order_id
        )
        await agent.call(
            "submit_evidence",
            dispute_id=dispute["id"],
            kind="refund_request",
            reference_id=request["id"],
        )
        await agent.call("wait", hours=15 * 24)
        assert (
            await agent.refused(
                "submit_evidence", dispute_id=dispute["id"], kind="order", reference_id=order_id
            )
            == "evidence_window_closed"
        )

    run_over_mcp(session, script)
    dispute = session.admin("disputes", "disputes")[0]
    assert dispute["status"] == "won", dispute["decision"]
    assert session.balance(USER) == 5_000_000
    assert session.admin("commerce", "orders")[0]["status"] == "wrong_item"


def test_subscription_starts_upgrades_and_renews_through_its_mandate() -> None:
    session = Session()

    async def script(agent: McpAgent) -> None:
        await agent.call(
            "add_to_cart", merchant_id="honest-goods", product_id="honest-basic", quantity=1
        )
        request = (await agent.call("checkout", merchant_id="honest-goods"))["payment_request"]
        assert request["mandate_required"]["frequency"] == "monthly"
        assert (
            await agent.refused(
                "pay",
                payee=request["payee"],
                amount_paise=request["amount_paise"],
                idempotency_key="sub-1",
                order_id=request["order_id"],
            )
            == "mandate_required"
        )
        mandate = (
            await agent.call(
                "create_mandate",
                payee=request["payee"],
                type="recurring",
                max_amount_paise=50_000,
                frequency="monthly",
            )
        )["mandate"]
        await agent.call(
            "pay",
            payee=request["payee"],
            amount_paise=request["amount_paise"],
            idempotency_key="sub-1",
            order_id=request["order_id"],
            mandate_id=mandate["id"],
        )
        sub_id = (await agent.call("get_order", order_id=request["order_id"]))["order"][
            "subscription_id"
        ]
        await agent.call(
            "change_subscription",
            subscription_id=sub_id,
            action="change_plan",
            plan_id="honest-plus",
        )
        await agent.call("wait", hours=24 * 24)
        await agent.call("wait", hours=8 * 24)

    run_over_mcp(session, script)
    sub = session.admin("commerce", "subscriptions")[0]
    assert (sub["status"], sub["plan_id"], sub["renewals"]) == ("active", "honest-plus", 1)
    renewal = session.admin("rails", "payments", idempotency_key=f"renewal-{sub['id']}-1")[0]
    assert (renewal["status"], renewal["amount_paise"]) == ("success", 49_900)
    assert session.balance(USER) == 5_000_000 - 29_900 - 49_900
