"""The LLM-judge backend: a general chat model asked for the decision JSON.

The model is anything that speaks :class:`ChatModel`. The sandbox gives it
``runner.guarding.ChatAdapter`` over the runner's LiteLLM adapter, the same one the
agent uses, so any LiteLLM model id works (``<provider>/<model>``). The id is a config
value with no default (``configs/judge-a.yaml``, ``judge-b.yaml``). The system prompt is
versioned (``prompts/judge-v<n>.md``) and recorded as the result's model version.

A provider failure, a context overflow or an answer that is not a valid decision fails
closed (``ask_user``, confidence 0) with the error recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from guard.actions import ActionsRegistry, actions
from guard.decision import (
    BackendResult,
    DecisionError,
    DecisionRequest,
    fail_closed,
    parse_decision,
)
from guard.render import render

from .questions import DECISION_OPTIONS

PROMPTS = Path(__file__).parent / "prompts"


@dataclass(frozen=True, slots=True)
class ChatAnswer:
    """What one chat call returned."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatModelError(Exception):
    """A chat call failed; ``code`` is what the judge fails closed with."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChatModel(Protocol):
    """A chat model the judge can ask. Raises :class:`ChatModelError` when a call fails."""

    id: str

    def ask(self, messages: list[dict[str, str]]) -> ChatAnswer:
        """One call, no tools."""
        ...


def system_prompt(version: str, reasons: dict[str, str]) -> str:
    """The judge's system prompt, with the request world's reason codes filled in."""
    path = PROMPTS / f"judge-{version}.md"
    if not path.exists():
        msg = f"no judge prompt {version!r} at {path}"
        raise FileNotFoundError(msg)
    text = path.read_text(encoding="utf-8")
    listing = "\n".join(f"- {code}: {description}" for code, description in reasons.items())
    for decision, description in DECISION_OPTIONS.items():
        text = text.replace(f"{{{decision}}}", description)
    return text.replace("{reasons}", listing)


class LLMJudge:
    """A chat model as a guard."""

    def __init__(
        self,
        name: str,
        model: ChatModel,
        *,
        prompt_version: str = "v1",
        registry: ActionsRegistry = actions,
    ) -> None:
        self.name = name
        self.model = model
        self.prompt_version = prompt_version
        self._registry = registry

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Ask the model once."""
        reasons = self._registry.get(request.world).reason_vocabulary()
        messages = [
            {"role": "system", "content": system_prompt(self.prompt_version, reasons)},
            {"role": "user", "content": render(request)},
        ]
        version = f"judge-{self.prompt_version}"
        try:
            answer = self.model.ask(messages)
        except ChatModelError as error:
            return fail_closed(self.name, error.code, model=self.model.id)
        used_in, used_out = answer.input_tokens, answer.output_tokens
        try:
            decision = parse_decision(answer.text)
        except DecisionError as error:
            return fail_closed(
                self.name,
                error.code,
                model=self.model.id,
                model_version=version,
                input_tokens=used_in,
                output_tokens=used_out,
            )
        return BackendResult(
            decision=decision,
            backend=self.name,
            model=self.model.id,
            model_version=version,
            input_tokens=used_in,
            output_tokens=used_out,
        )


__all__ = ["PROMPTS", "ChatAnswer", "ChatModel", "ChatModelError", "LLMJudge", "system_prompt"]
