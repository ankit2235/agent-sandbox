"""Counterparts in a run: the other agents of a multi-agent task (:mod:`kit.roles`).

``sandbox run --counterpart scripted`` (the default) plays every role with the world's own
scripted player; ``--counterpart <model id>`` plays them with a LiteLLM model, told the
generic counterpart prompt (``prompts/counterpart/v1.md``) and the task's brief for the role.
Either way the counterpart acts through its own MCP session, listing only its role's tools,
so its calls are ordinary steps of the episode's trace.

Turns follow the episode (:class:`kit.roles.TurnTaking`): before the agent's first model call
and after each of its tool calls, every role the episode says is awaited acts. A scripted
player makes the calls :meth:`~kit.roles.TurnTaking.scripted_turn` returns. A model gets up to
:data:`TURN_CALLS` model calls a turn, stopping early once its role is no longer awaited or it
calls no tool, and at most the task's ``max_steps`` model calls in a run. Its own text is not
kept; its player, prompt, usage and calls go to the record's :class:`CounterpartSummary`.

:class:`ScriptedReplay` is the other half, for ``sandbox verify``: the recorded calls are
replayed in order, and at every point where the runner played the counterparts, the scripted
player's calls are worked out again and must be the ones recorded (DECISIONS 265).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from importlib import resources
from typing import Any, overload

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel, ConfigDict, Field

from kit.episode import BaseEpisode
from kit.mcp_bridge import McpBridge
from kit.roles import TurnTaking
from kit.types import TaskSpec, ToolCall, Usage
from kit.world import WorldSpec

from .agent import (
    PROMPT_VERSION,
    PROMPTS,
    CallModel,
    Caps,
    StopReason,
    combined,
    observation_text,
    over_budget,
    refused,
    tool_arguments,
    tool_definitions,
)
from .models import ContextWindowExceeded, Message

SCRIPTED = "scripted"
"""``--counterpart`` value for the world's own scripted players (the default)."""
PROMPT_NAME = "counterpart"
TURN_CALLS = 4
"""Model calls a model counterpart gets in one turn."""
TURN_TEXT = "It is your turn to act as the {role}. Check what is new with your tools, then act."


def counterpart_prompt() -> tuple[str, str]:
    """The generic counterpart prompt, and its id (``counterpart/v1``)."""
    path = resources.files(PROMPTS) / PROMPT_NAME / f"{PROMPT_VERSION}.md"
    return f"{PROMPT_NAME}/{PROMPT_VERSION}", path.read_text(encoding="utf-8")


class CounterpartSummary(BaseModel):
    """What the counterparts of one run did: ``result.json``'s ``counterpart``."""

    model_config = ConfigDict(extra="forbid")

    player: str
    """``scripted``, or the model id that played them."""
    prompt: str | None = None
    """The counterpart prompt's id, for a model."""
    turns: int = 0
    """Turns they were given: each time a role had something to answer."""
    tool_calls: int = 0
    refused_calls: int = 0
    """Calls never applied: arguments that were not an object, or the agent's own tools."""
    model_turns: int = 0
    usage: Usage = Field(default_factory=Usage)
    cost_known: bool = True
    stopped: str | None = None
    """Why a model counterpart stopped playing early (``context_window``), if it did."""


def turn_taking(episode: object) -> TurnTaking:
    """The episode as :class:`~kit.roles.TurnTaking`, or a clear error."""
    if not isinstance(episode, TurnTaking):
        msg = f"{type(episode).__name__} has counterpart roles but is not kit.roles.TurnTaking"
        raise TypeError(msg)
    return episode


class Counterparts:
    """Every counterpart of one in-process multi-agent episode, for ``run_agent``."""

    def __init__(
        self,
        spec: WorldSpec,
        task: TaskSpec,
        episode: BaseEpisode,
        player: str,
        *,
        caps: Caps,
        call_model: CallModel | None = None,
    ) -> None:
        if (player == SCRIPTED) != (call_model is None):
            msg = "a model counterpart needs a model, and only it"
            raise ValueError(msg)
        self._spec = spec
        self._task = task
        self._episode = episode
        self._turns = turn_taking(episode)
        self._caps = caps
        self._call_model = call_model
        owned = {
            role.name: [t for t in role.tools if t in task.allowed_tools] for role in spec.roles
        }
        # Each role this task gives tools to; a role with none here takes no part.
        self._roles = {name: tools for name, tools in owned.items() if tools}
        self._sessions: dict[str, ClientSession] = {}
        self._definitions: dict[str, list[Message]] = {}
        self._conversations: dict[str, list[Message]] = {}
        self._prompt = counterpart_prompt() if call_model is not None else None
        self._limit = task.max_steps
        self._over: StopReason | None = None
        self.summary = CounterpartSummary(
            player=player, prompt=None if self._prompt is None else self._prompt[0]
        )

    @property
    def tools(self) -> frozenset[str]:
        """Every counterpart tool: the agent under test may not call these."""
        return frozenset(self._spec.counterpart_tools())

    @property
    def usage(self) -> Usage:
        """What the counterpart models have used."""
        return self.summary.usage

    @property
    def over_budget(self) -> StopReason | None:
        """The cap both agents together went over, once they have."""
        return self._over

    @property
    def done(self) -> bool:
        """Whether the episode has ended."""
        return self._episode.done

    @asynccontextmanager
    async def connected(self) -> AsyncIterator[object]:
        """One MCP session per role, listing that role's tools only."""
        async with AsyncExitStack() as stack:
            for role, tools in self._roles.items():
                listed = self._task.model_copy(update={"allowed_tools": tools})
                bridge = McpBridge(
                    self._episode, self._episode.router, listed, name=f"counterpart-{role}"
                )
                session = await stack.enter_async_context(
                    create_connected_server_and_client_session(bridge.server())
                )
                self._sessions[role] = session
                self._definitions[role] = tool_definitions((await session.list_tools()).tools)
            yield self
        self._sessions.clear()

    async def respond(self, spent: Usage) -> None:
        """Let every awaited role act once; ``spent`` is the agent's usage so far."""
        for role in await self._ask(self._turns.awaiting):
            if self._episode.done or self._over is not None:
                return
            self.summary.turns += 1
            if self._call_model is None:
                for call in await self._ask(partial(self._turns.scripted_turn, role)):
                    if self._episode.done:
                        return
                    await self._sessions[role].call_tool(call.tool, call.args)
                    self.summary.tool_calls += 1
            elif self.summary.stopped is None:
                await self._model_turn(role, spent)

    async def _model_turn(self, role: str, spent: Usage) -> None:
        assert self._call_model is not None and self._prompt is not None
        if role not in self._conversations:
            brief = await self._ask(partial(self._turns.brief, role))
            self._conversations[role] = [
                {"role": "system", "content": self._prompt[1]},
                {"role": "user", "content": brief},
            ]
        messages = self._conversations[role]
        messages.append({"role": "user", "content": TURN_TEXT.format(role=role)})
        allowed = set(self._roles[role])
        others = frozenset(self._spec.tool_names) - allowed
        for _ in range(TURN_CALLS):
            if self.summary.model_turns >= self._limit:
                return
            try:
                reply = await anyio.to_thread.run_sync(
                    self._call_model, list(messages), self._definitions[role]
                )
            except ContextWindowExceeded:
                self.summary.stopped = "context_window"
                return
            self.summary.model_turns += 1
            self.summary.usage = combined(self.summary.usage, reply.usage)
            self.summary.cost_known = self.summary.cost_known and reply.cost_known
            self._over = over_budget(combined(spent, self.summary.usage), self._caps)
            if self._over is not None:
                return
            messages.append(reply.assistant_message())
            if not reply.tool_calls:
                return
            for call in reply.tool_calls:
                arguments = tool_arguments(call.arguments)
                refusal = refused(call.name, arguments, others)
                if refusal is not None:
                    self.summary.refused_calls += 1
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": refusal})
                    continue
                outcome = await self._sessions[role].call_tool(call.name, arguments)
                self.summary.tool_calls += 1
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": observation_text(outcome)}
                )
                if self._episode.done:
                    return
            if role not in await self._ask(self._turns.awaiting):
                return

    @staticmethod
    async def _ask[Answer](read: Callable[[], Answer]) -> Answer:
        """Ask the episode something off the event loop: its services are synchronous."""
        return await anyio.to_thread.run_sync(read)


class ScriptedReplay(Sequence[ToolCall]):
    """A run's recorded calls for :func:`kit.scripted.run_script`, re-deriving the counterparts'.

    ``run_script`` builds call ``n`` only once the episode has applied every call before it,
    so at each point where the runner played the counterparts (before the first call, and
    after each of the agent's calls) the scripted players' calls are worked out from the
    replayed state, one role at a time, as the runner did, and compared with the recording.
    The first difference is reported in :attr:`problems`; the replay goes on unchanged.
    """

    def __init__(
        self, calls: Sequence[ToolCall], episode: Any, counterpart_tools: set[str]
    ) -> None:
        self._calls = list(calls)
        self._episode = episode
        self._turns = turn_taking(episode)
        self._tools = counterpart_tools
        self._roles: list[str] = []
        self._pending: list[ToolCall] = []
        self._played: set[int] = set()
        self.problems: list[str] = []

    def __len__(self) -> int:
        return len(self._calls)

    @overload
    def __getitem__(self, index: int) -> ToolCall: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[ToolCall]: ...

    def __getitem__(self, index: int | slice) -> ToolCall | Sequence[ToolCall]:
        if isinstance(index, slice):
            msg = "replayed calls are built one at a time"
            raise TypeError(msg)
        self._expect(index)
        if index >= len(self._calls):
            if not self.problems and (self._pending or self._roles):
                self._problem(index, "the trace ends where the scripted counterpart still acts")
            raise IndexError(index)
        call = self._calls[index]
        if self.problems:
            return call
        if self._pending:
            want = self._pending.pop(0)
            if (call.tool, call.args) != (want.tool, want.args):
                self._problem(
                    index,
                    f"the scripted counterpart calls {want.tool} {_short(want.args)}, "
                    f"the trace has {call.tool} {_short(call.args)}",
                )
        elif call.tool in self._tools:
            self._problem(
                index, f"{call.tool} is a counterpart call the scripted one does not make"
            )
        return call

    def _expect(self, index: int) -> None:
        """At a point where the runner played the counterparts, work out what they do."""
        at_turn = index == 0 or (
            index - 1 < len(self._calls) and self._calls[index - 1].tool not in self._tools
        )
        if at_turn and index not in self._played and not self._pending and not self._roles:
            self._played.add(index)
            if not self._episode.done:
                self._roles = list(self._turns.awaiting())
        while not self._pending and self._roles and not self._episode.done:
            self._pending = list(self._turns.scripted_turn(self._roles.pop(0)))
        if self._episode.done:
            self._roles, self._pending = [], []

    def _problem(self, index: int, text: str) -> None:
        self.problems.append(f"call {index + 1}: {text}")


def _short(args: dict[str, Any]) -> str:
    text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 120 else text[:117] + "..."


__all__ = [
    "SCRIPTED",
    "TURN_CALLS",
    "CounterpartSummary",
    "Counterparts",
    "ScriptedReplay",
    "counterpart_prompt",
    "turn_taking",
]
