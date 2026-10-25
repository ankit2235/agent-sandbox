"""The reference agent: a tool-calling loop that reaches the world only over MCP.

The episode is served by the kit's :class:`kit.mcp_bridge.McpBridge` and the
agent talks to it through a real MCP client session (the SDK's in-memory
transport), so it sees what any MCP client would: the task's allowed tools and
their schemas, and an observation per call. Every call goes through
:meth:`kit.world.Episode.step`, so the trace, simulated time, events and the
step cap are the same as for a scripted run.

The loop, until the episode is done or a cap is hit:

1. ask the model, with the fixed system prompt, the instruction and the
   conversation so far; record its text and usage as an agent message;
2. stop if a token or dollar cap is now exceeded (the over-budget turn's tool
   calls are not applied), or if it called no tool (it is done);
3. apply its tool calls in order over MCP and send each observation back.

Arguments that are not a JSON object never reach the world: the model is told
``malformed_arguments`` and the turn still counts towards the turn cap.

With a guard (``sandbox run --guard``), the agent's client talks to a
:class:`guard.mcp_proxy.GuardProxy` in front of the bridge instead: every call to a
guarded tool is decided first, and a held call never reaches the world.

The loop reaches the episode through an :class:`EpisodeLink`: :class:`LocalEpisode` (in
process, the in-memory transport) or :class:`runner.isolation.ContainerEpisode` (a
locked-down container, over stdio).

In a multi-agent task (:mod:`kit.roles`) the loop is given :class:`Counterparts`
(:mod:`runner.counterpart`): they act before the agent's first model call and after each of
its tool calls that reached the world, and the agent's calls to a counterpart's tools are
refused before they reach it, like malformed arguments. ``--max-tokens`` and ``--max-cost``
count both agents' usage.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Protocol

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from opentelemetry import trace as otel
from opentelemetry.trace import Tracer

from guard.mcp_proxy import Check, GuardProxy
from kit.episode import BaseEpisode
from kit.mcp_bridge import McpBridge
from kit.types import TaskSpec, Usage

from .models import ContextWindowExceeded, Message, ModelReply, ProviderError

PROMPTS = "runner.prompts"
PROMPT_VERSION = "v1"
DEFAULT_PROMPT = "default"

StopReason = str
"""Why a run ended: ``agent_finished``, ``terminal_tool``, ``max_steps``, ``max_turns``,
``max_tokens``, ``max_cost`` or ``context_window``."""


def system_prompt(world: str) -> tuple[str, str]:
    """The fixed system prompt for a world, and its id (``payments/v1``).

    A world without its own prompt gets the generic one. Prompts never carry
    task-specific hints; changing one means a new version file.
    """
    root = resources.files(PROMPTS)
    for name in (world, DEFAULT_PROMPT):
        path = root / name / f"{PROMPT_VERSION}.md"
        if path.is_file():
            return f"{name}/{PROMPT_VERSION}", path.read_text(encoding="utf-8")
    msg = f"no system prompt for {world!r} and no default prompt"
    raise FileNotFoundError(msg)


@dataclass(slots=True)
class Caps:
    """Hard limits for one run. ``None`` means no limit beyond the task's own step cap."""

    max_steps: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None

    def steps_for(self, task: TaskSpec) -> int:
        """Model turns allowed: the task's step cap, or a lower one from the command line."""
        return min(task.max_steps, self.max_steps) if self.max_steps else task.max_steps


@dataclass(slots=True)
class AgentResult:
    """What one agent loop did."""

    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
    model_turns: int = 0
    tool_calls: int = 0
    malformed_calls: int = 0
    cost_known: bool = True


CallModel = Callable[[Sequence[Message], Sequence[Message]], ModelReply]


class Counterparts(Protocol):
    """The other agents of a multi-agent episode, as the agent loop drives them."""

    @property
    def tools(self) -> frozenset[str]:
        """Their tools: the agent under test may not call these."""
        ...

    @property
    def usage(self) -> Usage:
        """What their models have used so far."""
        ...

    @property
    def over_budget(self) -> StopReason | None:
        """``max_tokens`` or ``max_cost`` once both agents together went over a cap."""
        ...

    @property
    def done(self) -> bool:
        """Whether the episode has ended (their calls count towards its step cap)."""
        ...

    def connected(self) -> AbstractAsyncContextManager[object]:
        """Their MCP sessions, open for the run."""
        ...

    async def respond(self, spent: Usage) -> None:
        """Let every counterpart that has something to answer act; ``spent`` is the agent's."""
        ...


class EpisodeLink(Protocol):
    """What the agent loop needs from an episode, wherever it runs."""

    @property
    def step_count(self) -> int:
        """Tool calls the world has applied."""
        ...

    def session(self, guard: Check | None) -> AbstractAsyncContextManager[ClientSession]:
        """An MCP client session to the task's tools, behind the guard if one is given."""
        ...

    def add_agent_message(self, text: str, usage: Usage | None) -> None:
        """Record what the model said in the episode's trace."""
        ...

    def tool_called(self) -> None:
        """One tool call was answered."""
        ...


class LocalEpisode:
    """An episode in this process: the kit's bridge over the in-memory MCP transport.

    ``listed`` is the task as the agent's session lists it: in a multi-agent task, without
    the counterparts' tools.
    """

    def __init__(
        self, episode: BaseEpisode, task: TaskSpec, listed: TaskSpec | None = None
    ) -> None:
        self.episode = episode
        self.task = task
        self.listed = listed or task

    @property
    def step_count(self) -> int:
        """Tool calls the episode has applied."""
        return self.episode.step_count

    def session(self, guard: Check | None) -> AbstractAsyncContextManager[ClientSession]:
        """A session to the bridge, or to a guard proxy in front of it."""
        bridge = McpBridge(
            self.episode, self.episode.router, self.listed, name=f"sandbox-{self.episode.world}"
        )
        return _session(bridge, guard)

    def add_agent_message(self, text: str, usage: Usage | None) -> None:
        """Append the message to the trace at the episode's current simulated time."""
        self.episode.trace.add_agent_message(self.episode.clock.now(), text, usage=usage)

    def tool_called(self) -> None:
        """Nothing to do: the episode counts its own steps."""


def tool_definitions(listed: Sequence[types.Tool]) -> list[Message]:
    """MCP tool definitions in the function-calling format LiteLLM takes."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in listed
    ]


def observation_text(result: types.CallToolResult) -> str:
    """What the model is sent for one tool call: the observation as JSON."""
    content = result.structuredContent
    if content is None:
        msg = "the sandbox bridge always returns the observation as structured content"
        raise RuntimeError(msg)
    shown = {key: content[key] for key in ("text", "data", "error", "sim_time") if key in content}
    return json.dumps(shown, ensure_ascii=False, sort_keys=True)


def tool_arguments(raw: str) -> dict[str, Any] | None:
    """A tool call's arguments, or ``None`` when they are not a JSON object."""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def run_agent(
    episode: EpisodeLink,
    task: TaskSpec,
    instruction: str,
    call_model: CallModel,
    *,
    prompt: str,
    caps: Caps,
    tracer: Tracer,
    guard: Check | None = None,
    counterparts: Counterparts | None = None,
) -> AgentResult:
    """Drive one episode with a model until it stops, the episode ends or a cap is hit.

    The MCP session runs in a task group, which wraps any error in an exception
    group; a single error is unwrapped so callers can catch it by its type.
    """
    try:
        return await _drive(
            episode,
            task,
            instruction,
            call_model,
            prompt=prompt,
            caps=caps,
            tracer=tracer,
            guard=guard,
            counterparts=counterparts,
        )
    except BaseExceptionGroup as group:
        raise _sole(group) from None


def _sole(group: BaseExceptionGroup[BaseException]) -> BaseException:
    error: BaseException = group
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


async def _drive(
    episode: EpisodeLink,
    task: TaskSpec,
    instruction: str,
    call_model: CallModel,
    *,
    prompt: str,
    caps: Caps,
    tracer: Tracer,
    guard: Check | None = None,
    counterparts: Counterparts | None = None,
) -> AgentResult:
    result = AgentResult(stop_reason="agent_finished")
    limit = caps.steps_for(task)
    others = counterparts.connected() if counterparts is not None else nullcontext()
    async with episode.session(guard) as client, others:
        tools = tool_definitions((await client.list_tools()).tools)
        messages: list[Message] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": instruction},
        ]
        if counterparts is not None:
            await counterparts.respond(result.usage)
            if counterparts.over_budget is not None:
                result.stop_reason = counterparts.over_budget
                return result
        while True:
            if result.model_turns >= limit:
                result.stop_reason = "max_turns"
                break
            with tracer.start_as_current_span("model.complete") as span:
                try:
                    reply = await anyio.to_thread.run_sync(call_model, list(messages), tools)
                except ContextWindowExceeded:
                    result.stop_reason = "context_window"
                    break
                span.set_attribute("gen_ai.usage.input_tokens", reply.usage.prompt_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", reply.usage.completion_tokens)
                span.set_attribute("sandbox.cost_usd", reply.usage.cost_usd)
                span.set_attribute("sandbox.tool_calls", len(reply.tool_calls))
            result.model_turns += 1
            _add_usage(result, reply)
            episode.add_agent_message(reply.text, reply.usage)
            if caps.max_cost is not None and not reply.cost_known:
                msg = f"the price of {task.id}'s model is unknown, so --max-cost cannot be enforced"
                raise ProviderError(msg, retriable=False)
            over = _over_budget(result, caps, counterparts.usage if counterparts else None)
            if over is not None:
                result.stop_reason = over
                break
            if not reply.tool_calls:
                result.stop_reason = "agent_finished"
                break
            messages.append(reply.assistant_message())
            ended = await _apply(
                client, episode, reply, messages, result, tracer, limit, counterparts
            )
            if ended is not None:
                if counterparts is not None and counterparts.over_budget is not None:
                    result.stop_reason = counterparts.over_budget
                    break
                capped = ended == "capped" or episode.step_count >= task.max_steps
                result.stop_reason = "max_steps" if capped else "terminal_tool"
                break
    return result


@asynccontextmanager
async def _session(bridge: McpBridge, guard: Check | None) -> AsyncIterator[ClientSession]:
    """The agent's MCP session: to the bridge, or to a guard proxy in front of it."""
    async with create_connected_server_and_client_session(bridge.server()) as upstream:
        if guard is None:
            yield upstream
            return
        proxy = GuardProxy(upstream, guard, name=f"guard-{bridge.name}")
        async with create_connected_server_and_client_session(proxy.server()) as client:
            yield client


async def _apply(
    client: ClientSession,
    episode: EpisodeLink,
    reply: ModelReply,
    messages: list[Message],
    result: AgentResult,
    tracer: Tracer,
    limit: int,
    counterparts: Counterparts | None = None,
) -> str | None:
    """Apply one turn's tool calls in order. Returns ``done``, ``capped`` or ``None``.

    After each call that reached the world, the counterparts answer; their calls count
    towards the episode's step cap, and a cap they push both agents over ends the run.
    """
    blocked = counterparts.tools if counterparts is not None else frozenset()
    for call in reply.tool_calls:
        if result.tool_calls >= limit:
            return "capped"
        arguments = tool_arguments(call.arguments)
        refusal = refused(call.name, arguments, blocked)
        if refusal is not None:
            result.malformed_calls += 1
            messages.append({"role": "tool", "tool_call_id": call.id, "content": refusal})
            continue
        assert arguments is not None  # refused() turns away anything else
        with tracer.start_as_current_span("tool.call") as span:
            span.set_attribute("sandbox.tool", call.name)
            outcome = await client.call_tool(call.name, arguments)
            episode.tool_called()
            observation = outcome.structuredContent or {}
            error = observation.get("error")
            if error:
                span.set_attribute("sandbox.error_code", str(error.get("code")))
                span.set_status(otel.Status(otel.StatusCode.ERROR, str(error.get("code"))))
            span.set_attribute("sandbox.sim_time", str(observation.get("sim_time")))
        result.tool_calls += 1
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": observation_text(outcome)}
        )
        if observation.get("done"):
            return "done"
        if counterparts is not None:
            await counterparts.respond(result.usage)
            if counterparts.over_budget is not None or counterparts.done:
                return "done"
    return None


def refused(name: str, arguments: dict[str, Any] | None, blocked: frozenset[str]) -> str | None:
    """The tool message for a call that never reaches the world, or ``None`` to send it.

    Arguments that are not a JSON object, and another role's tools, are refused here.
    """
    if arguments is None:
        code, message = "malformed_arguments", "tool arguments must be a JSON object"
    elif name in blocked:
        code, message = "tool_not_allowed", f"{name!r} belongs to another agent in this task"
    else:
        return None
    return json.dumps({"error": {"code": code, "message": message}})


def _add_usage(result: AgentResult, reply: ModelReply) -> None:
    total = result.usage
    result.usage = Usage(
        prompt_tokens=total.prompt_tokens + reply.usage.prompt_tokens,
        completion_tokens=total.completion_tokens + reply.usage.completion_tokens,
        cost_usd=total.cost_usd + reply.usage.cost_usd,
    )
    result.cost_known = result.cost_known and reply.cost_known


def _over_budget(result: AgentResult, caps: Caps, extra: Usage | None = None) -> StopReason | None:
    return over_budget(result.usage if extra is None else combined(result.usage, extra), caps)


def combined(first: Usage, second: Usage) -> Usage:
    """Two agents' usage together."""
    return Usage(
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
        cost_usd=first.cost_usd + second.cost_usd,
    )


def over_budget(usage: Usage, caps: Caps) -> StopReason | None:
    """The cap ``usage`` is over, if any."""
    if (
        caps.max_tokens is not None
        and usage.prompt_tokens + usage.completion_tokens > caps.max_tokens
    ):
        return "max_tokens"
    if caps.max_cost is not None and usage.cost_usd > caps.max_cost:
        return "max_cost"
    return None


__all__ = [
    "PROMPT_VERSION",
    "AgentResult",
    "CallModel",
    "Caps",
    "Counterparts",
    "EpisodeLink",
    "LocalEpisode",
    "combined",
    "observation_text",
    "over_budget",
    "refused",
    "run_agent",
    "system_prompt",
    "tool_arguments",
    "tool_definitions",
]
