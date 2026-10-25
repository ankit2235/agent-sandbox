"""What a world tells the guard and the training code about its high-impact actions.

A world ships a module and registers it through the ``agent_sandbox.training``
entry-point group under its own name::

    [project.entry-points."agent_sandbox.training"]
    payments = "worlds.payments.training"

The module imports nothing from the guard or the training package (a world must stay
installable without them) and defines, as plain values:

- ``HIGH_IMPACT``: the tools the guard stands in front of and whose attempts become
  examples;
- ``ASK_TOOL``: the tool the agent asks the user with;
- ``CAUSED_BY_ACTION``: the failure types an action can cause (a check that starts
  failing with one of these right after an attempt is that attempt's doing);
- ``ALWAYS_BLOCK``: those failure types that are blocked even when asking would do;
- ``policy(task) -> dict``: the delegated policy facts a task gives (mandates, caps);
- ``context(task, state, tool, args) -> dict``: the world state relevant to one
  attempt, read just before it, never the task's ground truth;
- ``OPAQUE_ARGS`` (optional): argument names whose values the run authors chose freely
  (an idempotency key such as ``kettle-retry``); in a request each value becomes a
  neutral token, numbered by first use, so a key can say nothing about the label;
- ``RULES``: the world's deterministic checks, as dicts with ``id``, ``decision``
  (``block`` or ``ask_user``), ``summary`` and ``check(request: dict) -> bool``
  (true when the attempt breaks the rule; ``request`` is a
  :class:`guard.DecisionRequest` as JSON). The ``rules_only`` backend applies all of
  them; a policy pack names the ones that are hard rules;
- ``guard_fields(request: dict) -> dict`` (optional): the policy fields of one attempt
  (``amount``, ``counterparty``, ``category``, ``mandate``), read from the request;
- ``guard_delegation(request: dict) -> dict`` (optional): the per-tool limits the task
  delegated (``{"pay": {"budget": {"per_action": ...}}}``), in the policy file's
  ``actions`` format, read from the request: its ``policy`` (``policy(task)``) and, for a
  limit the task changes while it runs, the current value in its ``state``.
"""

from __future__ import annotations

import importlib.metadata as metadata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from guard.decision import REASON_RE
from kit.failures import failure_types
from kit.types import TaskSpec
from kit.world import StateView, worlds

ENTRY_POINT_GROUP = "agent_sandbox.training"
REQUIRED = (
    "HIGH_IMPACT",
    "ASK_TOOL",
    "CAUSED_BY_ACTION",
    "ALWAYS_BLOCK",
    "policy",
    "context",
    "RULES",
)
RULE_DECISIONS = ("block", "ask_user")

Context = Callable[[TaskSpec, StateView, str, dict[str, Any]], dict[str, Any]]
Policy = Callable[[TaskSpec], dict[str, Any]]
Fields = Callable[[dict[str, Any]], dict[str, Any]]
Delegation = Callable[[dict[str, Any]], dict[str, Any]]


def _no_fields(request: dict[str, Any]) -> dict[str, Any]:
    return {}


def _no_delegation(request: dict[str, Any]) -> dict[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class Rule:
    """One deterministic check of the rules-only baseline."""

    id: str
    decision: str
    summary: str
    check: Callable[[dict[str, Any]], bool]


@dataclass(frozen=True, slots=True)
class WorldActions:
    """One world's high-impact actions and how to label and describe them."""

    world: str
    high_impact: frozenset[str]
    ask_tool: str
    caused_by_action: frozenset[str]
    always_block: frozenset[str]
    policy: Policy
    context: Context
    rules: tuple[Rule, ...]
    opaque_args: tuple[str, ...] = ()
    fields: Fields = _no_fields
    delegation: Delegation = _no_delegation

    def reason_vocabulary(self) -> dict[str, str]:
        """The reason codes a backend may give for this world: id -> description."""
        vocabulary = {code: failure_types.get(code).description for code in self.caused_by_action}
        vocabulary |= {rule.id: rule.summary for rule in self.rules}
        return dict(sorted(vocabulary.items()))


class TrainingError(ValueError):
    """A world's module is missing something or contradicts its WorldSpec."""


def from_module(world: str, module: ModuleType | Any) -> WorldActions:
    """Check a world's module and wrap it."""
    missing = [name for name in REQUIRED if not hasattr(module, name)]
    if missing:
        msg = f"{world}: the training module lacks {missing}"
        raise TrainingError(msg)
    spec = worlds.get(world)
    high_impact = frozenset(module.HIGH_IMPACT)
    unknown = sorted(high_impact - set(spec.tool_names))
    if unknown or module.ASK_TOOL not in spec.tool_names:
        msg = f"{world}: unknown tools {unknown or [module.ASK_TOOL]}"
        raise TrainingError(msg)
    caused = frozenset(module.CAUSED_BY_ACTION)
    always = frozenset(module.ALWAYS_BLOCK)
    for code in sorted(caused | always):
        if code not in failure_types:
            msg = f"{world}: {code!r} is not a registered failure type"
            raise TrainingError(msg)
    if not always <= caused:
        msg = f"{world}: ALWAYS_BLOCK must be a subset of CAUSED_BY_ACTION"
        raise TrainingError(msg)
    rules = tuple(_rule(world, raw) for raw in module.RULES)
    ids = [rule.id for rule in rules]
    if len(set(ids)) != len(ids):
        msg = f"{world}: rule ids repeat"
        raise TrainingError(msg)
    return WorldActions(
        world=world,
        high_impact=high_impact,
        ask_tool=str(module.ASK_TOOL),
        caused_by_action=caused,
        always_block=always,
        policy=module.policy,
        context=module.context,
        rules=rules,
        opaque_args=tuple(getattr(module, "OPAQUE_ARGS", ())),
        fields=getattr(module, "guard_fields", _no_fields),
        delegation=getattr(module, "guard_delegation", _no_delegation),
    )


def _rule(world: str, raw: Mapping[str, Any]) -> Rule:
    rule = Rule(
        id=str(raw["id"]),
        decision=str(raw["decision"]),
        summary=str(raw["summary"]),
        check=raw["check"],
    )
    if not REASON_RE.match(rule.id) or not rule.id.startswith(f"{world}."):
        msg = f"{world}: rule id {rule.id!r} must be a dotted id in the {world} namespace"
        raise TrainingError(msg)
    if rule.decision not in RULE_DECISIONS or not callable(rule.check):
        msg = f"{world}: rule {rule.id} needs a decision in {RULE_DECISIONS} and a check"
        raise TrainingError(msg)
    return rule


class ActionsRegistry:
    """Every installed world's training module, found through entry points."""

    def __init__(self) -> None:
        self._worlds: dict[str, WorldActions] = {}
        self._discovered = False

    def register(self, actions: WorldActions) -> WorldActions:
        """Add a world's actions directly (tests do)."""
        self._worlds[actions.world] = actions
        return actions

    def discover(self) -> None:
        """Load every installed world's training module once."""
        if self._discovered:
            return
        for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
            self._worlds.setdefault(entry.name, from_module(entry.name, entry.load()))
        self._discovered = True

    def get(self, world: str) -> WorldActions:
        """One world's actions."""
        self.discover()
        try:
            return self._worlds[world]
        except KeyError:
            msg = f"world {world!r} registers no training module; have {sorted(self._worlds)}"
            raise KeyError(msg) from None

    def names(self) -> list[str]:
        """Worlds with a training module."""
        self.discover()
        return sorted(self._worlds)


actions = ActionsRegistry()

__all__ = [
    "ENTRY_POINT_GROUP",
    "ActionsRegistry",
    "Rule",
    "TrainingError",
    "WorldActions",
    "actions",
    "from_module",
]
