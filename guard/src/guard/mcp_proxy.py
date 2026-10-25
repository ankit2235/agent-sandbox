"""The guard between an agent and any MCP server: :class:`GuardProxy`.

The proxy is an MCP server whose tools are the upstream server's (listed through an MCP
client session to it). Each call goes to ``check(tool, args)`` first, off the event loop:

- ``None`` (a tool the guard does not stand in front of) or an ``allow`` outcome: the call
  is forwarded unchanged and the upstream result returned unchanged;
- ``block`` or ``ask_user``: the call is held. It never reaches the upstream server, and
  the agent gets a tool error, ``guard.block`` or ``guard.ask_user``, with the reason
  codes. On ask_user the agent is told the action needs the user's confirmation; the
  guard never asks the user itself.

:func:`serve_stdio` runs the proxy over stdin/stdout in front of an upstream server started
as a subprocess (``sandbox-guard proxy``). Needs the ``mcp`` extra.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from guard.engine import Outcome

Check = Callable[[str, dict[str, Any]], Outcome | None]

MESSAGES = {
    "block": "The guard blocked this action; it was not applied.",
    "ask_user": (
        "This action needs the user's confirmation before it can run; it was not applied. "
        "Ask the user if you want to go ahead."
    ),
}


def refusal(outcome: Outcome) -> types.CallToolResult:
    """The tool error the agent gets for a held call."""
    message = MESSAGES[outcome.decision]
    error = {
        "code": f"guard.{outcome.decision}",
        "message": message,
        "reason_codes": outcome.reason_codes,
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{message} {json.dumps(error)}")],
        structuredContent={"text": message, "error": error},
        isError=True,
    )


class GuardProxy:
    """An MCP server in front of an upstream MCP session, deciding each call first."""

    def __init__(self, upstream: ClientSession, check: Check, *, name: str = "guard") -> None:
        self.upstream = upstream
        self.check = check
        self.name = name
        self.held: list[Outcome] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Decide one call, then forward it or hold it."""
        outcome = await anyio.to_thread.run_sync(self.check, tool, dict(arguments))
        if outcome is not None and outcome.decision != "allow":
            self.held.append(outcome)
            return refusal(outcome)
        return await self.upstream.call_tool(tool, arguments)

    def server(self) -> Server[Any, Any]:
        """A low-level MCP server whose handlers are this proxy."""
        server: Server[Any, Any] = Server(self.name)

        @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return list((await self.upstream.list_tools()).tools)

        @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await self.call(name, arguments)

        return server


async def serve_stdio(command: str, args: list[str], check: Check) -> None:
    """Start the upstream server as a subprocess and serve the proxy on stdin/stdout."""
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(
            stdio_client(StdioServerParameters(command=command, args=args))
        )
        upstream = await stack.enter_async_context(ClientSession(read, write))
        await upstream.initialize()
        server = GuardProxy(upstream, check).server()
        async with stdio_server() as (server_read, server_write):
            await server.run(server_read, server_write, server.create_initialization_options())


__all__ = ["MESSAGES", "Check", "GuardProxy", "refusal", "serve_stdio"]
