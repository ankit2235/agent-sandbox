"""The LLM-judge baseline: a general chat model asked for the decision JSON.

The model is reached through :class:`runner.models.LiteLLMModel`, the same adapter the
runner's agent uses, so any LiteLLM model id works (``<provider>/<model>``). The id is
a config value with no default (``configs/judge-a.yaml``, ``judge-b.yaml``). The system
prompt is versioned (``prompts/judge-v<n>.md``) and recorded as the result's model
version. Tests use :class:`runner.models.FakeModel`.

A provider failure, a context overflow or an answer that is not a valid decision fails
closed (``ask_user``, confidence 0) with the error recorded.
"""

from __future__ import annotations

from pathlib import Path

from guard.decision import (
    BackendResult,
    DecisionError,
    DecisionRequest,
    fail_closed,
    parse_decision,
)
from runner.models import ContextWindowExceeded, Model, ProviderError

from ..render import render
from ..world import ActionsRegistry, actions
from .questions import DECISION_OPTIONS

PROMPTS = Path(__file__).parent / "prompts"


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
        model: Model,
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
            reply = self.model.complete(messages, [])
        except ProviderError:
            return fail_closed(self.name, "provider_error", model=self.model.id)
        except ContextWindowExceeded:
            return fail_closed(self.name, "context_window", model=self.model.id)
        used_in, used_out = reply.usage.prompt_tokens, reply.usage.completion_tokens
        try:
            decision = parse_decision(reply.text)
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


__all__ = ["PROMPTS", "LLMJudge", "system_prompt"]
