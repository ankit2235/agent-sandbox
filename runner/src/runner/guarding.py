"""The guard in sandbox runs: ``sandbox run --guard <policy> --backend <name>``.

For each run a fresh :class:`guard.Guard` stands between the agent's MCP client and the
world's tools (:class:`guard.mcp_proxy.GuardProxy` in front of the kit's bridge). Every
call to a guarded tool (the world's ``HIGH_IMPACT`` tools and every tool the policy
names) is decided first, from the request the world's own module describes
(:mod:`guard.requests`); a held call never reaches the world, and the agent is told why.

The hard rules use the task's own delegated limits (the world's ``guard_delegation``, from
each request's ``policy(task)`` facts and the current state); the pack's numeric limits
are left out (``pack_limits=False``), and where both apply the stricter wins (DECISIONS).
Each run writes ``decisions.jsonl``, a hash-chained decision log, and its ``result.json``
keeps the chain's head and length.

This module also adapts the runner's chat models to the guard's LLM judge.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from guard.actions import ActionsRegistry, WorldActions, actions
from guard.backends.config import JudgeConfig, build_backend, describe, load_config
from guard.backends.llm_judge import ChatAnswer, ChatModelError
from guard.canonical import digest
from guard.decision import DecisionBackend
from guard.engine import Guard, Outcome
from guard.policy import Policy, PolicyError, load_policy
from guard.records import DecisionLog, read_log, verify_chain
from guard.requests import request_for
from kit.episode import BaseEpisode
from kit.trace import Trace
from kit.types import TaskSpec, ToolCall

from .models import ContextWindowExceeded, LiteLLMModel, Model, ProviderError

DECISIONS_FILE = "decisions.jsonl"


class ChatAdapter:
    """A runner model as the judge's :class:`guard.backends.llm_judge.ChatModel`."""

    def __init__(self, model: Model) -> None:
        self.model = model
        self.id = model.id

    def ask(self, messages: list[dict[str, str]]) -> ChatAnswer:
        """One call, no tools; provider failures become :class:`ChatModelError`."""
        try:
            reply = self.model.complete(messages, [])
        except ProviderError as error:
            raise ChatModelError("provider_error", str(error)) from error
        except ContextWindowExceeded as error:
            raise ChatModelError("context_window", str(error)) from error
        return ChatAnswer(
            text=reply.text,
            input_tokens=reply.usage.prompt_tokens,
            output_tokens=reply.usage.completion_tokens,
        )


def judge_chat_model(config: JudgeConfig) -> ChatAdapter:
    """The judge's chat model: LiteLLM, as the agent uses."""
    assert config.model is not None
    return ChatAdapter(
        LiteLLMModel(config.model, temperature=config.temperature, timeout=config.timeout_s)
    )


class GuardRunConfig(BaseModel):
    """What a guarded run directory records about its guard; resuming must match it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: str
    """The policy as given on the command line: a pack name or a file."""
    policy_id: str
    policy_version: int
    policy_hash: str
    backend: str
    backend_config: dict[str, Any] = Field(default_factory=dict)
    """The backend's config (it never holds a key), or just its name for an injected one."""


class GuardSummary(BaseModel):
    """What one run's ``result.json`` keeps about its decisions."""

    model_config = ConfigDict(extra="forbid")

    decisions: int = 0
    allowed: int = 0
    blocked: int = 0
    asked: int = 0
    head: str | None = None
    """The hash of the last decision record: a removed last record shows against it."""


class GuardSetup:
    """A policy and a backend, shared by every run of a ``sandbox run``."""

    def __init__(
        self,
        policy: Policy,
        backend: DecisionBackend,
        *,
        policy_source: str,
        backend_config: dict[str, Any] | None = None,
        registry: ActionsRegistry = actions,
    ) -> None:
        self.policy = policy
        self.backend = backend
        self.registry = registry
        self.config = GuardRunConfig(
            policy=policy_source,
            policy_id=policy.id,
            policy_version=policy.version,
            policy_hash=policy.hash,
            backend=backend.name,
            backend_config=backend_config or {"name": backend.name},
        )

    @classmethod
    def from_names(
        cls, policy: str, backend: str, overrides: list[str] | None = None
    ) -> GuardSetup:
        """From the command line: a pack name or policy file, and a backend config."""
        config = load_config(backend, overrides)
        built = build_backend(config, chat=judge_chat_model)
        return cls(
            load_policy(policy), built, policy_source=policy, backend_config=describe(config)
        )

    def for_run(self, task: TaskSpec, episode: BaseEpisode, directory: Path) -> RunGuard:
        """A fresh guard for one run, with the task's own delegation and an empty log."""
        world = self.registry.get(task.world)
        path = directory / DECISIONS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")  # every guarded run has a log, even an empty one
        guard = Guard(
            self.policy,
            self.backend,
            checks={rule.id: rule for rule in world.rules},
            log=DecisionLog(path),
            delegation=world.delegation,
            pack_limits=False,
        )
        return RunGuard(guard, task, episode, world)


class RunGuard:
    """One run's guard: decides each guarded call from the episode as it stands."""

    def __init__(
        self, guard: Guard, task: TaskSpec, episode: BaseEpisode, world: WorldActions
    ) -> None:
        self.guard = guard
        self.task = task
        self.episode = episode
        self.world = world
        self.guarded = frozenset(world.high_impact) | frozenset(guard.policy.actions)
        self.outcomes: list[Outcome] = []

    def check(self, tool: str, args: dict[str, Any]) -> Outcome | None:
        """Decide one call; None for a tool the guard does not stand in front of."""
        if tool not in self.guarded:
            return None
        episode = self.episode
        call = ToolCall(tool=tool, args=args)
        context = self.world.context(self.task, episode.state, tool, args)
        request = request_for(
            self.task, episode.trace, call, context, self.world, episode.clock.now()
        )
        fields = self.world.fields(request.model_dump(mode="json"))
        outcome = self.guard.decide(request, fields, args=args)
        self.outcomes.append(outcome)
        return outcome

    def summary(self) -> GuardSummary:
        """The counts and the chain head for ``result.json``."""
        decided = [outcome.decision for outcome in self.outcomes]
        head = self.guard.log.head if self.guard.log is not None else None
        return GuardSummary(
            decisions=len(decided),
            allowed=decided.count("allow"),
            blocked=decided.count("block"),
            asked=decided.count("ask_user"),
            head=head.hash if head is not None else None,
        )


GuardCheck = Callable[[str, dict[str, Any]], Outcome | None]


def guarded_tools(config: GuardRunConfig, world: str) -> tuple[frozenset[str] | None, list[str]]:
    """The tools a guarded run's guard stood in front of, from its policy (checked by hash)."""
    try:
        policy = load_policy(config.policy)
    except (FileNotFoundError, PolicyError) as error:
        return None, [f"the run's policy {config.policy!r} cannot be loaded: {error}"]
    if policy.hash != config.policy_hash:
        return None, [f"the policy {config.policy!r} changed since the run (hash differs)"]
    return frozenset(actions.get(world).high_impact) | frozenset(policy.actions), []


def verify_decisions(
    path: Path, summary: GuardSummary | None, trace: Trace, config: GuardRunConfig, world: str
) -> list[str]:
    """Problems with one guarded run's decisions: a broken chain, a head that does not match
    the run's record, a record under another policy, or allowed decisions that do not match
    the guarded calls the world received, one for one and in order.
    """
    if summary is None:
        return ["the run's result.json has no guard summary"]
    if not path.is_file():
        return [f"{path.name} is missing"]
    rows, problems = read_log(path)
    problems += verify_chain(rows, head=summary.head, count=summary.decisions)
    if problems:
        return [f"{path.name}: {problem}" for problem in problems]
    if any(row["policy"]["hash"] != config.policy_hash for row in rows):
        problems.append(f"{path.name}: a record names another policy than the run's")
    tools, unloadable = guarded_tools(config, world)
    if tools is None:
        return problems + unloadable
    allowed = [
        (row["action"]["tool"], row["action"]["args"]) for row in rows if row["decision"] == "allow"
    ]
    received = [
        (step.tool, digest(step.args or {})) for step in trace.tool_calls() if step.tool in tools
    ]
    if allowed != received:
        problems.append(
            f"{len(allowed)} allowed decisions but {len(received)} guarded calls reached the "
            "world, or they differ"
        )
    return problems


__all__ = [
    "DECISIONS_FILE",
    "ChatAdapter",
    "GuardCheck",
    "GuardRunConfig",
    "GuardSetup",
    "GuardSummary",
    "RunGuard",
    "guarded_tools",
    "judge_chat_model",
    "verify_decisions",
]
