"""The MCP proxy: in front of any MCP server, it forwards what the guard allows and holds the
rest, and the agent is told why. In memory in front of the kit's bridge, and end to end
over stdio with ``sandbox-guard proxy`` in front of the payments world's own server."""

import datetime as dt
import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from guard import DecisionLog, Guard, parse_policy, verify_chain
from guard.mcp_proxy import GuardProxy
from guard.records import read_log
from guard.wrap import request_from_call
from kit.clock import EPOCH, SimClock
from kit.mcp_bridge import McpBridge
from kit.rng import SeededRNG
from kit.tasks import load_tasks
from kit.world import worlds

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "worlds/payments/src/worlds/payments/tasks/rails/rails-gym-autopay-mandate.yaml"


def test_the_proxy_forwards_allowed_calls_and_holds_the_rest() -> None:
    spec = worlds.get("hello_world")
    task = load_tasks(spec.tasks_dir)[0]
    episode = spec.make_episode(task, SimClock(EPOCH), SeededRNG(task.seed))
    episode.reset(task)
    policy = parse_policy(
        {
            "format": "guard-policy-v1",
            "id": "hello",
            "version": 1,
            "default": "allow",
            "actions": {"increment": {"budget": {"per_action": 2}}},
        }
    )
    log = DecisionLog()
    guard = Guard(policy, log=log)

    def check(tool: str, args: dict):
        request = request_from_call(
            world="hello_world",
            instruction=task.instruction,
            tool=tool,
            args=args,
            now=episode.clock.now(),
        )
        by = args.get("by")
        return guard.decide(request, {"amount": by if isinstance(by, int) else None})

    async def drive() -> list[dict]:
        bridge = McpBridge(episode, episode.router, task)
        async with create_connected_server_and_client_session(bridge.server()) as upstream:
            proxy = GuardProxy(upstream, check)
            async with create_connected_server_and_client_session(proxy.server()) as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                assert "increment" in tools
                results = []
                for by in (1, 5, "two"):
                    result = await client.call_tool("increment", {"by": by})
                    results.append(result.structuredContent or {})
                results.append((await client.call_tool("get_count", {})).structuredContent or {})
                return results

    try:
        allowed, held, missing, count = anyio.run(drive)
    finally:
        episode.close()
    assert "error" not in allowed or not allowed["error"]
    assert held["error"]["code"] == "guard.block"
    assert held["error"]["reason_codes"] == ["guard.policy.over-per-action-budget"]
    assert missing["error"]["code"] == "guard.ask_user"
    assert missing["error"]["reason_codes"] == ["guard.policy.missing-amount"]
    assert "confirmation" in missing["text"]
    assert count["data"] == {"count": 1} or "1" in json.dumps(count)
    assert [record.decision for record in log.records] == ["allow", "block", "ask_user", "allow"]


def test_sandbox_guard_proxy_over_stdio_in_front_of_the_payments_server(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    command = [
        "-m",
        "guard.cli",
        "proxy",
        "--policy",
        "payments/default",
        "--world",
        "payments",
        "--instruction",
        "Set up autopay for my gym, at most 1,200 a month.",
        "--now",
        "2026-01-01T09:00:00+00:00",
        "--log",
        str(log),
        "--",
        sys.executable,
        "-m",
        "worlds.payments.tools.server",
        str(TASK),
    ]

    async def drive() -> list[dict]:
        params = StdioServerParameters(command=sys.executable, args=command)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"pay", "get_balance", "create_mandate"} <= names
            out = []
            for name, args in (
                ("get_balance", {}),
                (
                    "pay",
                    {"payee": "fitbox@sandbox", "amount_paise": 2000000, "idempotency_key": "k1"},
                ),
                (
                    "pay",
                    {"payee": "fitbox@sandbox", "amount_paise": 1100000, "idempotency_key": "k2"},
                ),
            ):
                out.append((await client.call_tool(name, args)).structuredContent or {})
            return out

    balance, over, confirm = anyio.run(drive)
    assert not balance.get("error")
    assert over["error"]["code"] == "guard.block"
    assert "guard.policy.over-per-action-budget" in over["error"]["reason_codes"]
    assert confirm["error"]["code"] == "guard.ask_user"
    assert confirm["error"]["reason_codes"] == ["guard.policy.confirm-above"]
    rows, problems = read_log(log)
    assert problems == [] and verify_chain(rows) == []
    assert [row["decision"] for row in rows] == ["block", "ask_user"]
    assert all(row["sim_time"] == "2026-01-01T09:00:00Z" for row in rows)
    assert dt.datetime.fromisoformat(rows[0]["sim_time"]).tzinfo is not None
