"""End to end through MCP: the hard task's golden run, driven only through ``tools/call``."""

from typing import Any

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from security_testkit import BY_ID, Session

from kit.world import load_grader
from worlds.security.graders.runs import GOLDEN, load_runs
from worlds.security.tools import TOOL_NAMES
from worlds.security.tools.server import bridge_for


def test_the_golden_run_passes_when_played_over_mcp() -> None:
    task = BY_ID["multi-maintenance-vs-lateral"]
    session = Session(task)
    steps = load_runs(task.id).runs[GOLDEN].steps
    listed: list[str] = []
    errors: list[Any] = []

    async def main() -> None:
        server = bridge_for(session.episode).server()
        async with create_connected_server_and_client_session(server) as client:
            listed.extend(tool.name for tool in (await client.list_tools()).tools)
            for step in steps:
                result = await client.call_tool(step.tool, step.args)
                if result.isError:
                    errors.append((step.tool, result.structuredContent))

    anyio.run(main)
    assert listed == TOOL_NAMES
    assert errors == []
    grade = load_grader(task.grader)(
        session.episode.state, session.episode.trace.grader_view(), task
    )
    assert grade.passed, [c for c in grade.checks if not c.passed]
