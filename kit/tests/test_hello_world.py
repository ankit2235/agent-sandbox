import pytest

from kit.examples.hello_world import task_source, world_spec
from kit.examples.hello_world.scripted import RUNS, golden_run
from kit.scripted import run_script
from kit.types import ToolCall

SPEC = world_spec()
TASKS = task_source().tasks


@pytest.fixture(params=[task.id for task in TASKS], ids=lambda value: value)
def task(request):
    return task_source().get(request.param)


def test_every_task_validates_against_the_world(task) -> None:
    SPEC.validate_task(task)


def test_golden_run_passes(task) -> None:
    run = run_script(SPEC, task, golden_run(task))
    assert run.grade is not None
    assert run.grade.passed, run.grade.failed_checks
    assert run.grade.score == 1.0
    assert run.grade.failure_type is None


@pytest.mark.parametrize("name", ["wrong_overshoots", "wrong_never_finishes"])
def test_plausible_wrong_runs_fail(task, name: str) -> None:
    run = run_script(SPEC, task, RUNS[name](task))
    assert run.grade is not None
    assert not run.grade.passed
    assert run.grade.failure_type is not None


def test_overshoot_is_reported_as_the_overshoot_failure(task) -> None:
    run = run_script(SPEC, task, RUNS["wrong_overshoots"](task))
    assert run.grade is not None
    # The second task has a ceiling well above the target, so the overshoot lands.
    assert run.grade.failure_type == "hello_world.overshoot"


def test_three_identical_runs_give_identical_bytes_and_scores(task) -> None:
    runs = [run_script(SPEC, task, golden_run(task)) for _ in range(3)]
    scores = {run.score for run in runs}
    states = {tuple(sorted(run.final_state.items())) for run in runs}
    traces = {run.trace.model_dump_json() for run in runs}
    assert len(scores) == 1
    assert len(states) == 1
    assert len(traces) == 1


def test_the_service_refuses_to_cross_its_ceiling() -> None:
    task = task_source().get("hello-respect-ceiling")
    run = run_script(
        SPEC,
        task,
        [ToolCall(tool="increment", args={"by": 100}), ToolCall(tool="finish")],
    )
    rejected = [step for step in run.trace.steps if step.error]
    assert rejected[0].error is not None
    assert rejected[0].error.code == "ceiling_exceeded"
    assert run.grade is not None
    assert not run.grade.passed


def test_running_out_of_steps_ends_the_episode() -> None:
    task = task_source().get("hello-count-to-three")
    calls = [ToolCall(tool="get_count")] * (task.max_steps + 3)
    run = run_script(SPEC, task, calls)
    assert run.steps == task.max_steps
    assert run.results[-1].info["cap_reached"] == "max_steps"
    assert run.grade is not None
    assert run.grade.failure_type == "hello_world.wrong_count"


def test_graders_see_actions_but_no_reasoning() -> None:
    task = task_source().get("hello-count-to-three")
    run = run_script(SPEC, task, golden_run(task))
    run.trace.add_agent_message(run.trace.steps[-1].sim_time, "secret reasoning")
    view = run.trace.grader_view()
    assert "secret reasoning" not in view.model_dump_json()
    assert view.count("increment") == 1
