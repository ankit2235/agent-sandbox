"""Backend configs: one YAML file per backend, with every model id and version pinned in it.

Packaged configs live in ``configs/<name>.yaml`` and are named by file stem. A config is
validated before anything runs; ``--set key=value`` on the command line fills or changes
one field (the judges' model ids have no default and must be given this way or in a
copy of the file). Keys come from the environment variables a config names, never from
a file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from guard.decision import DecisionBackend

from .llm_judge import ChatModel
from .system_one import ConfigError, secret

CONFIGS = Path(__file__).parent / "configs"


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    notes: str = ""


class RulesConfig(_Config):
    """The rules-only backend."""

    kind: Literal["rules_only"]


class JudgeConfig(_Config):
    """A general chat model through LiteLLM."""

    kind: Literal["llm_judge"]
    model: str | None = None
    """A LiteLLM model id such as ``<provider>/<model>``. No default: set it at run time."""
    prompt_version: str = "v1"
    temperature: float | None = 0.0
    timeout_s: float = Field(default=60.0, gt=0)


class SystemOneConfig(_Config):
    """Jev, Clef or a self-hosted Strands Decider."""

    kind: Literal["system_one"]
    endpoint: str
    """May contain ``{account}``, filled from ``account_env`` (Cloudflare)."""
    model: str | None = None
    pinned: str | None = None
    """The exact model and revision the endpoint serves, when the caller chooses it."""
    api_key_env: str | None = None
    account_env: str | None = None
    timeout_s: float = Field(default=10.0, gt=0)
    reason_threshold: float = Field(default=0.5, ge=0, le=1)


class OpenAIDecisionsConfig(_Config):
    """OpenAI's Decisions API."""

    kind: Literal["openai_decisions"]
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    endpoint: str = "https://api.openai.com/v1/decisions"
    timeout_s: float = Field(default=10.0, gt=0)
    reason_threshold: float = Field(default=0.5, ge=0, le=1)


class GuardModelConfig(_Config):
    """Our model, run locally with transformers."""

    kind: Literal["guard_model"]
    base_model: str
    revision: str
    adapter: str | None = None
    """The trained LoRA adapter's folder."""
    device: str = "cpu"
    dtype: Literal["float16", "bfloat16", "float32"] = "float32"


BackendConfig = Annotated[
    RulesConfig | JudgeConfig | SystemOneConfig | OpenAIDecisionsConfig | GuardModelConfig,
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[Any] = TypeAdapter(BackendConfig)


def _value(text: str) -> Any:
    loaded = yaml.safe_load(text)
    return text if loaded is None and text not in ("null", "~") else loaded


def load_config(name_or_path: str, overrides: list[str] | None = None) -> BackendConfig:
    """A packaged config by name, or a YAML file, with ``key=value`` overrides applied."""
    path = Path(name_or_path)
    if not path.exists():
        path = CONFIGS / f"{name_or_path}.yaml"
    if not path.exists():
        known = sorted(p.stem for p in CONFIGS.glob("*.yaml"))
        msg = f"no backend config {name_or_path!r}; packaged ones: {known}"
        raise FileNotFoundError(msg)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for item in overrides or []:
        key, sep, text = item.partition("=")
        if not sep or not key:
            msg = f"--set takes key=value, got {item!r}"
            raise ConfigError(msg)
        raw[key] = _value(text)
    config: BackendConfig = _ADAPTER.validate_python(raw)
    return config


def configs() -> list[str]:
    """The packaged backend configs' names."""
    return sorted(p.stem for p in CONFIGS.glob("*.yaml"))


ChatFactory = Callable[["JudgeConfig"], ChatModel]
"""Makes the judge's chat model from its config; the sandbox passes
``runner.guarding.judge_chat_model`` (LiteLLM). The guard itself calls no model library."""


def build_backend(config: BackendConfig, *, chat: ChatFactory | None = None) -> DecisionBackend:
    """A backend from its config. Raises :class:`ConfigError` for anything missing."""
    if isinstance(config, RulesConfig):
        from .rules_only import RulesOnlyBackend

        return RulesOnlyBackend(config.name)
    if isinstance(config, JudgeConfig):
        if not config.model:
            msg = (
                f"{config.name}: the judge's model id has no default; "
                "give it with --set model=<provider>/<model>"
            )
            raise ConfigError(msg)
        if chat is None:
            msg = f"{config.name}: a judge needs a chat model factory (chat=...)"
            raise ConfigError(msg)
        from .llm_judge import LLMJudge

        return LLMJudge(config.name, chat(config), prompt_version=config.prompt_version)
    if isinstance(config, SystemOneConfig):
        from .system_one import SystemOneBackend

        endpoint = config.endpoint
        if "{account}" in endpoint:
            account = secret(config.account_env) if config.account_env else None
            if not account:
                msg = f"{config.name}: the endpoint needs an account id; set account_env"
                raise ConfigError(msg)
            endpoint = endpoint.replace("{account}", account)
        return SystemOneBackend(
            config.name,
            endpoint=endpoint,
            model=config.model,
            api_key=secret(config.api_key_env),
            timeout_s=config.timeout_s,
            reason_threshold=config.reason_threshold,
        )
    if isinstance(config, OpenAIDecisionsConfig):
        from .openai_decisions import OpenAIDecisionsBackend

        key = secret(config.api_key_env)
        assert key is not None
        return OpenAIDecisionsBackend(
            config.name,
            model=config.model,
            api_key=key,
            endpoint=config.endpoint,
            timeout_s=config.timeout_s,
            reason_threshold=config.reason_threshold,
        )
    from .guard_model.backend import GuardModelBackend
    from .guard_model.hf import HFScorer

    scorer = HFScorer(
        config.base_model,
        config.revision,
        adapter=config.adapter,
        device=config.device,
        dtype=config.dtype,
    )
    return GuardModelBackend(config.name, scorer)


def describe(config: BackendConfig) -> dict[str, Any]:
    """What a result records about its backend: the config, which holds no secret
    (keys are only ever named by their environment variable).
    """
    data: dict[str, Any] = config.model_dump(mode="json")
    return data


__all__ = [
    "CONFIGS",
    "BackendConfig",
    "ChatFactory",
    "GuardModelConfig",
    "JudgeConfig",
    "OpenAIDecisionsConfig",
    "RulesConfig",
    "SystemOneConfig",
    "build_backend",
    "configs",
    "describe",
    "load_config",
]
