"""The OpenEnv / Hugging Face Space bundle in ``worlds/payments/openenv/``.

Needs the openenv extra (``uv sync --all-packages --extra openenv``); skipped without it.
The bundle is imported from its folder, as the Space's Docker image runs it.
"""

import importlib
import sys
from pathlib import Path

import pytest
import yaml

from kit.openenv_adapter import OPENENV_VERSION, openenv_available
from kit.scripted import load_runs
from kit.world import worlds

if not openenv_available():
    pytest.skip(f"needs openenv=={OPENENV_VERSION}", allow_module_level=True)

BUNDLE = Path(__file__).parents[1] / "openenv"


@pytest.fixture(scope="module")
def space():
    sys.path.insert(0, str(BUNDLE))
    try:
        environment = importlib.import_module("server.payments_env_environment")
        app = importlib.import_module("server.app")
        models = importlib.import_module("models")
        yield environment, app, models
    finally:
        sys.path.remove(str(BUNDLE))
        for name in [n for n in sys.modules if n == "models" or n.startswith("server")]:
            del sys.modules[name]


def test_the_manifest_names_the_app_and_a_reward_in_0_1() -> None:
    manifest = yaml.safe_load((BUNDLE / "openenv.yaml").read_text())
    assert manifest["spec_version"] == 1
    assert (manifest["type"], manifest["runtime"], manifest["app"]) == (
        "space",
        "fastapi",
        "server.app:app",
    )
    assert manifest["validation"]["reward"]["range"] == [0.0, 1.0]


def test_reset_lists_the_tasks_tools_and_finish(space) -> None:
    environment, _, _ = space
    env = environment.create_payments_environment()
    task = worlds.get("payments").task_source().get("rails-gym-autopay-mandate")
    observation = env.reset(task_id=task.id)
    names = [tool["name"] for tool in observation.tools]
    assert names == [*task.allowed_tools, "finish_episode"]
    assert "FitBox" in observation.text
    env.close()


def test_a_golden_run_then_finish_scores_1(space) -> None:
    environment, _, models = space
    spec = worlds.get("payments")
    env = environment.create_payments_environment()
    env.reset(task_id="rails-gym-autopay-mandate")
    golden = load_runs("rails-gym-autopay-mandate", spec.runs_dir).runs["golden"]
    for step in golden.steps:
        result = env.step(models.PaymentsAction(tool=step.tool, args=step.args))
        assert (result.done, result.reward) == (False, 0.0)
    final = env.step(models.PaymentsAction(tool="finish_episode", args={}))
    assert (final.done, final.reward) == (True, 1.0)
    assert final.metadata["grade"]["passed"] is True
    with pytest.raises(RuntimeError, match="has finished"):
        env.step(models.PaymentsAction(tool="list_mandates", args={}))
    env.reset(task_id="rails-gym-autopay-mandate")
    assert env.step(models.PaymentsAction(tool="finish_episode", args={})).reward < 1.0
    env.close()


def test_the_app_serves_health_schema_tasks_and_reset(space) -> None:
    from fastapi.testclient import TestClient

    _, app, _ = space
    client = TestClient(app.app)
    assert client.get("/health").status_code == 200
    schema = client.get("/schema").json()
    assert "tool" in str(schema)
    assert [split["name"] for split in client.get("/payments_env/splits").json()] == ["v0.1"]
    counted = client.post("/payments_env/num_tasks", json={"split": "v0.1"}).json()
    assert counted == {"num_tasks": 50}
    reply = client.post("/reset", json={"task_id": "rails-gym-autopay-mandate"}).json()
    tools = [tool["name"] for tool in reply["observation"]["tools"]]
    assert tools[-1] == "finish_episode"


def test_the_client_reads_the_servers_replies() -> None:
    """The bundle installs as the package ``payments_env``; load it under that name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "payments_env", BUNDLE / "__init__.py", submodule_search_locations=[str(BUNDLE)]
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules["payments_env"] = package
    try:
        spec.loader.exec_module(package)
        client = package.PaymentsEnv(base_url="http://127.0.0.1:1")
        action = package.PaymentsAction(tool="pay", args={"payee": "a@sandbox"})
        assert client._step_payload(action) == {"tool": "pay", "args": {"payee": "a@sandbox"}}
        tools = [{"name": "pay", "description": "Pay.", "input_schema": {}}]
        result = client._parse_result(
            {
                "observation": {
                    "text": "ok",
                    "sim_time": "2026-01-01T09:00:00+00:00",
                    "tools": tools,
                },
                "reward": 1.0,
                "done": True,
            }
        )
        assert (result.observation.text, result.observation.tools) == ("ok", tools)
        assert (result.reward, result.done) == (1.0, True)
        state = client._parse_state({"episode_id": "e", "step_count": 2, "task_id": "t"})
        assert (state.step_count, state.task_id) == (2, "t")
    finally:
        for name in [
            n for n in sys.modules if n == "payments_env" or n.startswith("payments_env.")
        ]:
            del sys.modules[name]
