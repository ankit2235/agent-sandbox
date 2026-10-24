"""Model adapters: LiteLLM for real models, and fake models so CI never calls an API.

Every model speaks the OpenAI chat format LiteLLM uses: a list of ``system``,
``user``, ``assistant`` (with ``tool_calls``) and ``tool`` messages in, one
:class:`ModelReply` out. The agent loop builds the messages itself from the
replies, so a fake and a real model see exactly the same conversation.

Model ids:

- ``fake:golden`` replays each task's golden run from the world's run files,
  filling ``$refs`` from the tool results it was sent; ``fake:wrong`` replays
  the task's first wrong run; ``fake:idle`` stops at once without a tool call.
  A stressed task's runs are built from its base task's; a hidden task's come
  from the private repository (:func:`task_runs`).
- anything else is a LiteLLM model id, e.g. ``openai/gpt-4.1-mini`` or
  ``ollama/llama3.2``. Needs the ``litellm`` extra.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from kit.scripted import Run, RunFile, load_runs, resolve_refs
from kit.types import TaskSpec, Usage
from kit.world import WorldSpec
from stress.core import base_id, scripted_runs

from . import DISTRIBUTION

FAKE_PREFIX = "fake:"
Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One tool call the model asked for. ``arguments`` is the raw JSON text it sent."""

    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ModelReply:
    """What one model call returned."""

    text: str = ""
    tool_calls: list[ToolRequest] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_known: bool = True
    """False when the provider's price for this model is unknown; ``usage.cost_usd`` is then 0."""

    def assistant_message(self) -> Message:
        """The reply as the ``assistant`` message the next call is sent."""
        message: Message = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class ProviderError(Exception):
    """A model call failed. ``retriable`` errors are retried with backoff by the runner."""

    def __init__(self, message: str, *, retriable: bool) -> None:
        super().__init__(message)
        self.retriable = retriable


class ContextWindowExceeded(Exception):
    """The conversation no longer fits the model. The run stops and is graded as it stands."""


class Model(Protocol):
    """A chat model that can call tools."""

    id: str

    def complete(self, messages: Sequence[Message], tools: Sequence[Message]) -> ModelReply:
        """One model call."""
        ...


# ---------------------------------------------------------------------- fakes
class FakeModel:
    """Returns scripted replies in order, for tests. An exception in the script is raised."""

    def __init__(
        self, turns: Sequence[ModelReply | Exception], model_id: str = "fake:script"
    ) -> None:
        self.id = model_id
        self._turns = list(turns)
        self.calls: list[list[Message]] = []

    def complete(self, messages: Sequence[Message], tools: Sequence[Message]) -> ModelReply:
        """The next scripted reply; a plain stop once the script runs out."""
        self.calls.append([dict(message) for message in messages])
        if not self._turns:
            return ModelReply(text="Done.")
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class ReplayModel:
    """Replays one of a task's scripted runs as tool calls, one call per turn.

    ``$name.path`` arguments are filled from the tool results in the conversation,
    exactly as the run player fills them from the trace, so ``fake:golden`` passes
    every task whose golden run passes, through the whole runner.
    """

    def __init__(self, model_id: str, run: Run | None) -> None:
        self.id = model_id
        self._run = run

    def complete(self, messages: Sequence[Message], tools: Sequence[Message]) -> ModelReply:
        """The run's next step, given the conversation so far."""
        issued = [
            call["id"]
            for message in messages
            if message["role"] == "assistant"
            for call in message.get("tool_calls") or []
        ]
        steps = self._run.steps if self._run is not None else []
        if len(issued) >= len(steps):
            return ModelReply(text="Done.")
        results = {
            message["tool_call_id"]: json.loads(message["content"])
            for message in messages
            if message["role"] == "tool"
        }
        saved: dict[str, Any] = {}
        for number, step in enumerate(steps[: len(issued)]):
            if step.save is None:
                continue
            result = results.get(issued[number])
            if result is None or result.get("error") is not None:
                saved[step.save] = None
            else:
                saved[step.save] = {"text": result.get("text", ""), **result.get("data", {})}
        step = steps[len(issued)]
        arguments = json.dumps(resolve_refs(step.args, saved), ensure_ascii=False)
        return ModelReply(
            tool_calls=[
                ToolRequest(id=f"call-{len(issued) + 1}", name=step.tool, arguments=arguments)
            ]
        )


def replay_model(model_id: str, spec: WorldSpec, task: TaskSpec) -> ReplayModel:
    """``fake:golden``, ``fake:wrong`` or ``fake:idle`` for one task."""
    which = model_id.removeprefix(FAKE_PREFIX)
    if which == "idle":
        return ReplayModel(model_id, None)
    if which not in ("golden", "wrong"):
        msg = f"unknown fake model {model_id!r}; use fake:golden, fake:wrong or fake:idle"
        raise ValueError(msg)
    if spec.runs_dir is None:
        msg = f"world {spec.name!r} ships no run files, so {model_id} cannot replay its tasks"
        raise ValueError(msg)
    runs = task_runs(spec, task)
    return ReplayModel(model_id, runs.runs["golden" if which == "golden" else runs.wrong[0]])


def task_runs(spec: WorldSpec, task: TaskSpec) -> RunFile:
    """A task's scripted runs: a hidden task's from the private repository, a stressed
    task's built from its base task's (:func:`stress.core.scripted_runs`), else the world's.
    """
    assert spec.runs_dir is not None
    if task.visibility == "hidden":
        from .hidden import private_root, runs_dir

        return load_runs(task.id, runs_dir(spec, private_root()))
    if task.stress:
        return scripted_runs(task, load_runs(base_id(task), spec.runs_dir))
    return load_runs(task.id, spec.runs_dir)


# -------------------------------------------------------------------- litellm
RETRIABLE = (
    "RateLimitError",
    "APIConnectionError",
    "Timeout",
    "InternalServerError",
    "ServiceUnavailableError",
    "BadGatewayError",
)
"""LiteLLM exception classes worth retrying: the provider, not the request, is at fault."""

FATAL = (
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "BadRequestError",
    "UnprocessableEntityError",
    "APIResponseValidationError",
    "APIError",
)
"""LiteLLM exception classes retrying cannot fix: a key, a model id or a request is wrong."""


class LiteLLMModel:
    """Any provider or local model, through ``litellm.completion``."""

    def __init__(
        self,
        model_id: str,
        *,
        temperature: float | None = 0.0,
        seed: int | None = None,
        timeout: float = 120.0,
        module: Any = None,
    ) -> None:
        self.id = model_id
        self.temperature = temperature
        self.seed = seed
        self.timeout = timeout
        self._litellm = module if module is not None else _import_litellm()

    def complete(self, messages: Sequence[Message], tools: Sequence[Message]) -> ModelReply:
        """One call; provider failures become :class:`ProviderError`."""
        litellm = self._litellm
        exceptions = litellm.exceptions
        retriable = tuple(getattr(exceptions, name) for name in RETRIABLE)
        fatal = tuple(getattr(exceptions, name) for name in FATAL)
        try:
            response = litellm.completion(
                model=self.id,
                messages=list(messages),
                tools=list(tools) or None,
                tool_choice="auto" if tools else None,
                temperature=self.temperature,
                seed=self.seed,
                timeout=self.timeout,
                num_retries=0,
                drop_params=True,
            )
        except exceptions.ContextWindowExceededError as error:
            raise ContextWindowExceeded(str(error)) from error
        except retriable as error:
            raise ProviderError(f"{type(error).__name__}: {error}", retriable=True) from error
        except fatal as error:
            raise ProviderError(f"{type(error).__name__}: {error}", retriable=False) from error
        return self._reply(response)

    def _reply(self, response: Any) -> ModelReply:
        message = response.choices[0].message
        calls = [
            ToolRequest(
                id=str(call.id),
                name=str(call.function.name),
                arguments=call.function.arguments or "{}",
            )
            for call in message.tool_calls or []
        ]
        usage = response.usage
        cost = self._cost(response)
        return ModelReply(
            text=message.content or "",
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                cost_usd=cost or 0.0,
            ),
            cost_known=cost is not None,
        )

    def _cost(self, response: Any) -> float | None:
        try:
            return float(self._litellm.completion_cost(completion_response=response))
        except Exception as error:  # litellm raises a bare Exception for a model with no price
            if "isn't mapped" not in str(error):
                raise
            return None


def _import_litellm() -> Any:
    try:
        import litellm
    except ImportError as error:
        msg = (
            "real models need the litellm extra: uv sync --all-packages --extra litellm "
            f"(or pip install '{DISTRIBUTION}[litellm]')"
        )
        raise ImportError(msg) from error
    return litellm


ModelFactory = Callable[[WorldSpec, TaskSpec], Model]


def model_factory(
    model_id: str, *, temperature: float | None = 0.0, seed: int | None = None
) -> ModelFactory:
    """Build a model for each run: a replay for ``fake:*``, otherwise LiteLLM."""
    if model_id.startswith(FAKE_PREFIX):
        return lambda spec, task: replay_model(model_id, spec, task)
    module = _import_litellm()
    return lambda spec, task: LiteLLMModel(
        model_id, temperature=temperature, seed=seed, module=module
    )


__all__ = [
    "FAKE_PREFIX",
    "ContextWindowExceeded",
    "FakeModel",
    "LiteLLMModel",
    "Message",
    "Model",
    "ModelFactory",
    "ModelReply",
    "ProviderError",
    "ReplayModel",
    "ToolRequest",
    "model_factory",
    "replay_model",
    "task_runs",
]
