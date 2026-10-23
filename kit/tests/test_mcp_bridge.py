"""The MCP bridge, proven on the hello-world example so the kit tests need no world."""

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from kit.clock import EPOCH, SimClock
from kit.examples.hello_world.episode import HelloWorldEpisode, build_router
from kit.mcp_bridge import McpBridge, call_result
from kit.rng import SeededRNG


@pytest.fixture
def bridge(make_task):
    def build(**overrides: object) -> McpBridge:
        task = make_task(**overrides)
        episode = HelloWorldEpisode(task, SimClock(EPOCH), SeededRNG(task.seed))
        episode.reset(task)
        return McpBridge(episode, build_router(), task, name="hello")

    return build


def test_tools_are_filtered_to_the_tasks_allowed_tools(bridge) -> None:
    assert [tool.name for tool in bridge(allowed_tools=["get_count"]).tool_definitions()] == [
        "get_count"
    ]
    assert [tool.name for tool in bridge(allowed_tools=[]).tool_definitions()] == [
        "get_count", "increment", "finish",
    ]  # fmt: skip


def test_a_call_goes_through_the_episode_and_its_trace(bridge) -> None:
    b = bridge()
    result = call_result(b.call("increment", {"by": 2}))
    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["data"] == {"count": 2}
    assert b.episode.trace.tool_calls("increment")[0].state_diff[0].after == 2


def test_a_refusal_is_an_error_result_and_is_still_recorded(bridge) -> None:
    b = bridge(allowed_tools=["get_count", "finish"])
    result = call_result(b.call("increment", {"by": 1}))
    assert result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["error"]["code"] == "tool_not_allowed"
    assert b.episode.trace.count("increment") == 1


def test_a_real_client_lists_and_calls_tools(bridge) -> None:
    b = bridge(allowed_tools=["get_count", "increment"])

    async def main() -> list:
        async with create_connected_server_and_client_session(b.server()) as client:
            listed = [tool.name for tool in (await client.list_tools()).tools]
            await client.call_tool("increment", {"by": 1})
            hidden = await client.call_tool("finish", {})
            return [listed, hidden.isError, hidden.structuredContent["error"]["code"]]

    listed, hidden_error, code = anyio.run(main)
    assert listed == ["get_count", "increment"]
    assert (hidden_error, code) == (True, "tool_not_allowed")
    assert b.episode.state["counter"].query("count") == {"count": 1}


def test_arguments_the_schema_refuses_still_reach_the_episode(bridge) -> None:
    b = bridge(max_steps=20)

    async def main() -> bool:
        async with create_connected_server_and_client_session(b.server()) as client:
            result = await client.call_tool("finish", {"unexpected": True})
            return result.isError

    # The schema forbids extra arguments, but the SDK's own check is off: the call
    # reaches the episode, which decides (hello-world's finish ignores them) and traces it.
    assert anyio.run(main) is False
    assert b.episode.trace.count("finish") == 1


def test_concurrent_calls_are_applied_one_at_a_time(bridge) -> None:
    b = bridge(max_steps=50)

    async def main() -> None:
        async with (
            create_connected_server_and_client_session(b.server()) as client,
            anyio.create_task_group() as group,
        ):
            for _ in range(20):
                group.start_soon(client.call_tool, "increment", {"by": 1})

    anyio.run(main)
    assert b.episode.state["counter"].query("count") == {"count": 20}
    assert [step.index for step in b.episode.trace.steps] == list(range(20))
