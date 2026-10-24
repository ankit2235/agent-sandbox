"""Helpers for the runner tests. Worlds are reached only through the kit's registry."""

import json
from collections.abc import Callable
from pathlib import Path

import yaml

from kit.world import worlds
from runner.models import FakeModel, ModelReply, ToolRequest
from runner.runs import RunConfig

HELLO_TASKS = Path(__file__).parents[2] / "kit/src/kit/examples/hello_world/tasks"


def call(name: str, arguments: dict | str | None = None, call_id: str | None = None) -> ToolRequest:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    return ToolRequest(id=call_id or f"call-{name}", name=name, arguments=raw)


def turn(*calls: ToolRequest, text: str = "", tokens: int = 0, cost: float = 0.0) -> ModelReply:
    reply = ModelReply(text=text, tool_calls=list(calls))
    reply.usage = reply.usage.model_copy(
        update={"prompt_tokens": tokens, "completion_tokens": 0, "cost_usd": cost}
    )
    return reply


def scripted(*turns: ModelReply | Exception) -> Callable[..., FakeModel]:
    """A model factory that gives every run the same script."""
    return lambda spec, task: FakeModel(list(turns))


def world_config(world: str, model: str = "fake:golden", **overrides: object) -> RunConfig:
    spec = worlds.get(world)
    payload: dict[str, object] = {
        "world": world,
        "tasks": f"{world}-{spec.version}",
        "model": model,
        "model_name": model,
        "provider": "fake",
    }
    payload.update(overrides)
    return RunConfig.model_validate(payload)


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_hidden_set(root: Path, world: str, count: int, monkeypatch) -> Path:
    """A private task set built at test time from public tasks; never written to the repo.

    Each task gets a canary, and the set's hash list is "published" to a file under
    ``root`` that :func:`runner.hidden.hash_file` is pointed at for the test.
    """
    from runner import hidden

    spec = worlds.get(world)
    folder = root / "hidden-repo" / world / spec.version
    tasks = []
    for task in spec.task_source().tasks[:count]:
        hidden_id = f"secret-{task.id}"
        secret = task.model_copy(
            update={
                "id": hidden_id,
                "visibility": "hidden",
                "tags": [*task.tags, hidden.canary(7, hidden_id)],
            }
        )
        tasks.append(secret)
        path = folder / secret.category / f"{hidden_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(secret.model_dump(mode="json")))
    published = root / "published" / f"hidden-{spec.version}.json"
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text(json.dumps(hidden.hash_list(spec, tasks)))
    original = hidden.hash_file
    monkeypatch.setattr(
        hidden, "hash_file", lambda s: published if s.name == world else original(s)
    )
    return root / "hidden-repo"
