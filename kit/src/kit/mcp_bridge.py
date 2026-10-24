"""Serving an episode's tools over MCP.

World-neutral: any world that builds its tools as a :class:`kit.episode.ToolRouter`
and its episode on :class:`kit.episode.BaseEpisode` gets an MCP server from this
module unchanged. Every call goes through :meth:`kit.world.Episode.step`, so the
allowed-tools check, simulated time, scheduled events and the trace step are the
same whether a scripted test, the OpenEnv adapter or an MCP client drives the run.

Needs the ``mcp`` extra: ``pip install 'agent-sandbox-kit[mcp]'``.

Two details matter for determinism:

- The SDK handles requests concurrently, but an episode is strictly sequential.
  :class:`McpBridge` holds a lock around every step, so calls apply one at a time,
  in the order they arrive.
- The SDK can validate arguments against the input schema before the handler
  runs. That is switched off: a call with bad arguments must still reach the
  episode, so it is refused by the world with a code *and* recorded in the trace.
"""

from __future__ import annotations

import threading
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from kit.episode import ToolRouter, ToolSpec
from kit.types import StepResult, TaskSpec, ToolCall
from kit.world import Episode


def call_result(result: StepResult) -> types.CallToolResult:
    """An episode step as an MCP tool result: the text, plus the observation as data."""
    observation = result.observation
    structured: dict[str, Any] = observation.model_dump(mode="json")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=observation.text)],
        structuredContent=structured,
        isError=observation.error is not None,
    )


class McpBridge:
    """One episode's tools, filtered to the task's ``allowed_tools``, as an MCP server."""

    def __init__(
        self, episode: Episode, router: ToolRouter, task: TaskSpec, *, name: str = "sandbox"
    ) -> None:
        self.episode = episode
        self.router = router
        self.task = task
        self.name = name
        self._lock = threading.Lock()

    @property
    def tools(self) -> list[ToolSpec]:
        """The tools this task may use, in the router's order. All of them if none are listed."""
        allowed = set(self.task.allowed_tools)
        return [
            self.router.get(name) for name in self.router.names if not allowed or name in allowed
        ]

    def tool_definitions(self) -> list[types.Tool]:
        """What ``tools/list`` returns."""
        return [
            types.Tool(name=tool.name, description=tool.description, inputSchema=tool.input_schema)
            for tool in self.tools
        ]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> StepResult:
        """Apply one tool call to the episode. Thread-safe; calls never overlap."""
        with self._lock:
            return self.episode.step(ToolCall(tool=name, args=dict(arguments or {})))

    async def call_async(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> types.CallToolResult:
        """Run a call off the event loop: services are reached synchronously."""
        result = await anyio.to_thread.run_sync(self.call, name, arguments)
        return call_result(result)

    def server(self) -> Server[Any, Any]:
        """A low-level MCP server whose two handlers are this bridge."""
        server: Server[Any, Any] = Server(self.name, instructions=self.task.instruction)

        @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return self.tool_definitions()

        @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await self.call_async(name, arguments)

        return server

    async def serve_stdio(self) -> None:
        """Serve over stdin/stdout until the client disconnects."""
        server = self.server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


__all__ = ["McpBridge", "call_result"]
