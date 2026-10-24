import pytest

from kit.examples.hello_world import task_source, world_spec
from kit.openenv_adapter import (
    OPENENV_VERSION,
    action_class,
    make_environment,
    openenv_available,
)
from kit.tasks import TaskSource

pytestmark = pytest.mark.openenv

if not openenv_available():
    pytest.skip(f"needs openenv=={OPENENV_VERSION}", allow_module_level=True)


def build_env():
    return make_environment(world_spec(), task_source())


def action(tool: str, **args):
    return action_class()(tool=tool, args=args)


def test_adapter_reports_the_worlds_task_splits() -> None:
    env = build_env()
    assert env.list_splits() == ["v0.1"]
    assert env.num_tasks("v0.1") == 2
    assert env.get_task("v0.1", 0)["id"] == "hello-count-to-three"
    with pytest.raises(IndexError):
        env.get_task("v0.1", 99)


def test_reset_returns_the_instruction_and_state() -> None:
    env = build_env()
    observation = env.reset(task_id="hello-count-to-three")
    assert "exactly 3" in observation.text
    assert env.state.world == "hello_world"
    assert env.state.step_count == 0
    env.close()


def test_a_full_episode_scores_through_the_adapter() -> None:
    env = build_env()
    env.reset(task_id="hello-count-to-three")
    env.step(action("get_count"))
    env.step(action("increment", by=3))
    final = env.step(action("finish"))
    assert final.done
    assert final.reward == 1.0
    assert final.metadata["grade"]["passed"] is True
    env.close()


def test_three_runs_through_the_adapter_give_the_same_score() -> None:
    scores = []
    for _ in range(3):
        env = build_env()
        env.reset(task_id="hello-respect-ceiling", seed=11)
        env.step(action("get_count"))
        env.step(action("increment", by=3))
        result = env.step(action("finish"))
        scores.append(result.reward)
        env.close()
    assert scores == [1.0, 1.0, 1.0]


def test_metadata_names_the_world_and_version() -> None:
    metadata = build_env().get_metadata()
    assert metadata.name == "hello_world"
    assert metadata.version == "v0.1"


def test_reset_lists_the_tools_the_task_allows() -> None:
    env = build_env()
    observation = env.reset(task_id="hello-count-to-three")
    names = [tool["name"] for tool in observation.tools]
    assert names == ["get_count", "increment", "finish"]
    assert all(set(tool) == {"name", "description", "input_schema"} for tool in observation.tools)
    assert observation.tools[1]["input_schema"]["properties"]["by"]["type"] == "integer"
    after = env.step(action("get_count"))
    assert after.tools == []
    env.close()


def test_a_task_naming_some_tools_lists_only_those() -> None:
    spec = world_spec()
    task = task_source().get("hello-count-to-three")
    narrowed = task.model_copy(update={"allowed_tools": ["get_count", "finish"]})
    env = make_environment(spec, TaskSource([narrowed]))
    observation = env.reset(task_id=narrowed.id)
    assert [tool["name"] for tool in observation.tools] == ["get_count", "finish"]
    env.close()
