"""Counterparts: other agents acting in a world beside the agent under test (K4).

A world may declare roles on its :class:`kit.world.WorldSpec`: a seller the user's agent
negotiates with, say. A role owns some of the world's tools; no other agent is offered them.
A task is multi-agent when its ``allowed_tools`` name a role's tool, and the agent under test
is offered only the rest.

Turns are the world's to decide. An episode of a world with roles implements
:class:`TurnTaking`: after each of the agent's tool calls (and before its first) the runner
asks which roles have something to answer, and plays each one, either with the world's own
scripted player (:meth:`TurnTaking.scripted_turn`) or with a model told the role's brief. Both
agents' calls go through :meth:`kit.world.Episode.step`, so they are ordinary steps of one
trace, and a replay of the trace's calls in order reproduces the episode.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import Field, field_validator

from kit.types import ID_RE, Strict, TaskSpec, ToolCall


class Role(Strict):
    """A counterpart role: an agent that is not under test, and the tools only it may call."""

    name: str
    description: str = ""
    tools: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not ID_RE.match(value):
            msg = f"role name {value!r} must match {ID_RE.pattern}"
            raise ValueError(msg)
        return value


@runtime_checkable
class TurnTaking(Protocol):
    """What a multi-agent episode tells the runner between the agent's calls."""

    def awaiting(self) -> list[str]:
        """The roles that have something to answer now, in the order they should act."""
        ...

    def brief(self, role: str) -> str:
        """What a model playing ``role`` is told about this task: its side of it only."""
        ...

    def scripted_turn(self, role: str) -> list[ToolCall]:
        """The calls the world's scripted player of ``role`` makes now. Reads, never writes."""
        ...


def role_tools(roles: Sequence[Role]) -> set[str]:
    """Every tool some role owns."""
    return {tool for role in roles for tool in role.tools}


def is_multi_agent(roles: Sequence[Role], task: TaskSpec) -> bool:
    """Whether a task allows any role's tool, so a counterpart acts in it."""
    return bool(set(task.allowed_tools) & role_tools(roles))


def agent_tools(roles: Sequence[Role], task: TaskSpec) -> list[str]:
    """The task's tools the agent under test is offered: every one no role owns."""
    owned = role_tools(roles)
    return [tool for tool in task.allowed_tools if tool not in owned]


def check_roles(roles: Sequence[Role], tool_names: Sequence[str]) -> None:
    """Roles have unique names, own only the world's tools, and never share one."""
    names = [role.name for role in roles]
    if len(set(names)) != len(names):
        msg = f"role names repeat: {names}"
        raise ValueError(msg)
    seen: dict[str, str] = {}
    for role in roles:
        unknown = sorted(set(role.tools) - set(tool_names))
        if unknown:
            msg = f"role {role.name!r} names tools the world does not have: {unknown}"
            raise ValueError(msg)
        for tool in role.tools:
            if tool in seen:
                msg = f"tool {tool!r} belongs to both {seen[tool]!r} and {role.name!r}"
                raise ValueError(msg)
            seen[tool] = role.name


__all__ = [
    "Role",
    "TurnTaking",
    "agent_tools",
    "check_roles",
    "is_multi_agent",
    "role_tools",
]
