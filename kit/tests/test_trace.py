import datetime as dt

from kit.clock import EPOCH
from kit.trace import DiffEntry, GraderTraceStep, Trace
from kit.types import ToolError, Usage


def build_trace() -> Trace:
    trace = Trace(run_id="run-1", task_id="t-1", world="hello_world")
    trace.add_agent_message(EPOCH, "I will read the counter first.", usage=Usage(prompt_tokens=10))
    trace.add_tool_call(
        EPOCH + dt.timedelta(seconds=1),
        "get_count",
        {},
        response={"count": 0},
    )
    trace.add_tool_call(
        EPOCH + dt.timedelta(seconds=2),
        "increment",
        {"by": 3},
        response={"count": 3},
        state_diff=[DiffEntry(op="update", path="counter/count", before=0, after=3)],
    )
    trace.add_tool_call(
        EPOCH + dt.timedelta(seconds=3),
        "increment",
        {"by": -1},
        error=ToolError(code="invalid_amount", message="by must be a positive integer"),
    )
    trace.add_event(EPOCH + dt.timedelta(seconds=4), "limit_change", {"daily_cap": 100})
    return trace


def test_steps_are_indexed_in_order() -> None:
    trace = build_trace()
    assert [step.index for step in trace.steps] == [0, 1, 2, 3, 4]


def test_grader_view_cannot_carry_reasoning() -> None:
    view = build_trace().grader_view()
    assert all(isinstance(step, GraderTraceStep) for step in view.steps)
    assert "agent_message" not in GraderTraceStep.model_fields
    assert [step.kind for step in view.steps] == ["tool_call", "tool_call", "tool_call", "event"]


def test_grader_view_keeps_actions_errors_and_diffs() -> None:
    view = build_trace().grader_view()
    assert view.count("increment") == 2
    assert view.tools_used() == {"get_count", "increment"}
    assert [event.event for event in view.events()] == ["limit_change"]
    rejected = [step for step in view.steps if step.error]
    assert rejected[0].error is not None
    assert rejected[0].error.code == "invalid_amount"
    assert view.tool_calls("increment")[0].state_diff[0].after == 3


def test_jsonl_roundtrip_is_lossless(tmp_path) -> None:
    trace = build_trace()
    path = trace.write_jsonl(tmp_path / "traces" / "run-1.jsonl")
    restored = Trace.read_jsonl(path)
    assert restored.model_dump() == trace.model_dump()
    assert path.read_text().splitlines()[0].startswith('{"kind": "header"')


def test_usage_is_recorded_on_agent_messages_only() -> None:
    trace = build_trace()
    assert trace.steps[0].usage is not None
    assert all(step.usage is None for step in trace.steps[1:])
