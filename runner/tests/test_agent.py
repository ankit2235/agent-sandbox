"""The agent loop, on the hello-world counter: every way a turn can go."""

import json
from pathlib import Path

from runner_testkit import call, scripted, turn

from kit.trace import Trace
from runner.agent import system_prompt
from runner.models import ContextWindowExceeded
from runner.runs import RESULT_FILE, RUNS_DIR, TRACE_FILE, RunRecord, execute, load_records


def run_one(tmp_path: Path, config, *turns) -> tuple[RunRecord, Trace]:
    out = tmp_path / "out"
    execute(config, out, factory=scripted(*turns))
    [record] = load_records(out)
    return record, Trace.read_jsonl(out / RUNS_DIR / record.run_id / TRACE_FILE)


def test_a_model_that_counts_to_three_and_finishes_passes(tmp_path, hello_config) -> None:
    record, trace = run_one(
        tmp_path,
        hello_config(),
        turn(call("increment", {"by": 2}), text="Two first."),
        turn(call("increment", {"by": 1}), call("finish", {"note": "done"})),
    )
    assert record.status == "completed"
    assert record.passed
    assert record.stop_reason == "terminal_tool"
    assert (record.steps, record.model_turns) == (3, 2)
    assert [step.kind for step in trace.steps] == [
        "agent_message", "tool_call", "agent_message", "tool_call", "tool_call",
    ]  # fmt: skip
    assert trace.steps[0].agent_message == "Two first."
    assert trace.run_id == record.run_id == "fake-script-hello-count-to-three-r1"


def test_a_text_reply_ends_the_run_and_it_is_graded_as_it_stands(tmp_path, hello_config) -> None:
    record, _ = run_one(tmp_path, hello_config(), turn(text="I think it is fine."))
    assert record.stop_reason == "agent_finished"
    assert record.status == "completed"
    assert not record.passed
    assert record.steps == 0


def test_malformed_arguments_never_reach_the_world(tmp_path, hello_config) -> None:
    record, trace = run_one(
        tmp_path,
        hello_config(),
        turn(call("increment", "{not json"), call("increment", "[1, 2]")),
        turn(call("increment", {"by": 3}), call("finish")),
    )
    assert record.malformed_calls == 2
    assert record.passed
    assert trace.count("increment") == 1


def test_the_model_is_told_about_malformed_arguments(tmp_path, hello_config) -> None:
    from runner.models import FakeModel

    model = FakeModel([turn(call("increment", "{bad", call_id="c1")), turn(text="stop")])
    execute(hello_config(), tmp_path / "out", factory=lambda spec, task: model)
    tool_message = model.calls[1][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "c1"
    assert json.loads(tool_message["content"])["error"]["code"] == "malformed_arguments"


def test_tools_the_task_does_not_allow_are_refused_and_recorded(tmp_path, hello_config, hello_dir):
    text = (hello_dir / "hello-count-to-three.yaml").read_text()
    (hello_dir / "hello-count-to-three.yaml").write_text(
        text.replace("allowed_tools: [get_count, increment, finish]", "allowed_tools: [increment]")
    )
    record, trace = run_one(
        tmp_path,
        hello_config(),
        turn(call("finish"), call("no_such_tool")),
        turn(text="stop"),
    )
    errors = [step.error.code for step in trace.tool_calls() if step.error]
    assert errors == ["tool_not_allowed", "unknown_tool"]
    assert record.steps == 2


def test_only_the_tasks_allowed_tools_are_offered(tmp_path, hello_config) -> None:
    from runner.models import FakeModel

    model = FakeModel([turn(text="stop")])
    execute(hello_config(), tmp_path / "out", factory=lambda spec, task: model)
    assert model.calls[0][0]["role"] == "system"
    assert model.calls[0][1] == {
        "role": "user",
        "content": "The counter starts at 0. Raise it to exactly 3, then call finish.",
    }


def test_a_token_cap_stops_before_the_turn_that_crossed_it_is_applied(tmp_path, hello_config):
    record, trace = run_one(
        tmp_path,
        hello_config(max_tokens=100),
        turn(call("increment", {"by": 1}), tokens=60),
        turn(call("increment", {"by": 2}), call("finish"), tokens=60),
    )
    assert record.stop_reason == "max_tokens"
    assert trace.count("increment") == 1
    assert record.usage.prompt_tokens == 120


def test_a_cost_cap_stops_the_run(tmp_path, hello_config) -> None:
    record, trace = run_one(
        tmp_path,
        hello_config(max_cost=0.01),
        turn(call("increment", {"by": 1}), cost=0.004),
        turn(call("increment", {"by": 2}), cost=0.007),
    )
    assert record.stop_reason == "max_cost"
    assert trace.count("increment") == 1
    assert round(record.usage.cost_usd, 3) == 0.011


def test_a_lower_step_cap_cuts_a_turn_short(tmp_path, hello_config) -> None:
    record, trace = run_one(
        tmp_path,
        hello_config(max_steps=2),
        turn(call("increment", {"by": 1}), call("increment", {"by": 1}), call("increment")),
    )
    assert record.stop_reason == "max_steps"
    assert trace.count("increment") == 2


def test_the_tasks_own_step_cap_ends_the_episode(tmp_path, hello_config) -> None:
    record, trace = run_one(tmp_path, hello_config(), *[turn(call("get_count"))] * 12)
    assert record.stop_reason == "max_steps"
    assert trace.count("get_count") == 8


def test_turns_without_a_valid_call_are_capped_too(tmp_path, hello_config) -> None:
    record, _ = run_one(tmp_path, hello_config(), *[turn(call("increment", "{"))] * 12)
    assert record.stop_reason == "max_turns"
    assert record.model_turns == 8


def test_a_full_context_window_stops_and_grades(tmp_path, hello_config) -> None:
    record, _ = run_one(
        tmp_path, hello_config(), turn(call("increment", {"by": 3})), ContextWindowExceeded("full")
    )
    assert record.stop_reason == "context_window"
    assert record.status == "completed"
    assert record.steps == 1


def test_a_result_file_is_written_last(tmp_path, hello_config) -> None:
    out = tmp_path / "out"
    execute(hello_config(), out, factory=scripted(turn(text="stop")))
    run_dir = next((out / RUNS_DIR).iterdir())
    assert sorted(path.name for path in run_dir.iterdir()) == [RESULT_FILE, TRACE_FILE]


def test_each_world_has_a_fixed_versioned_prompt() -> None:
    assert system_prompt("payments")[0] == "payments/v1"
    assert system_prompt("security")[0] == "security/v1"
    prompt_id, text = system_prompt("hello_world")
    assert prompt_id == "default/v1"
    assert "tool" in text
    assert "defensive" in system_prompt("security")[1]
