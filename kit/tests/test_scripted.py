"""The run-file player in kit.scripted, proven on the hello-world world."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kit.examples.hello_world import task_source, world_spec
from kit.scripted import Run, RunError, RunFile, load_runs, play, resolve_refs

TASK = task_source().get("hello-count-to-three")


def run(*steps: dict) -> Run:
    return Run.model_validate({"steps": list(steps)})


def test_a_golden_run_passes_and_refs_are_filled_from_earlier_results() -> None:
    result = play(
        world_spec(),
        TASK,
        run(
            {"tool": "increment", "args": {"by": 1}, "save": "first"},
            {"tool": "increment", "args": {"by": "$first.count"}, "expect": {"count": 2}},
            {"tool": "increment", "args": {"by": 1}},
            {"tool": "finish", "args": {}},
        ),
    )
    assert result.passed
    assert result.trace.tool_calls("increment")[1].args == {"by": 1}


def test_a_run_that_does_not_go_as_scripted_fails_loudly() -> None:
    with pytest.raises(RunError, match=r"step 1 \(increment\): expected"):
        play(
            world_spec(),
            TASK,
            run({"tool": "increment", "args": {"by": 1}, "expect": {"count": 9}}),
        )
    with pytest.raises(RunError, match="ended after step 1 of 2"):
        play(world_spec(), TASK, run({"tool": "finish"}, {"tool": "get_count"}))


def test_resolve_refs_reports_missing_paths_and_refused_steps() -> None:
    saved = {"co": {"order": {"id": "o-1", "lines": [{"sku": "a"}]}}, "bad": None}
    assert resolve_refs({"x": ["$co.order.lines.0.sku"]}, saved) == {"x": ["a"]}
    with pytest.raises(RunError, match="'missing' is not in the result"):
        resolve_refs("$co.order.missing", saved)
    with pytest.raises(RunError, match="was refused"):
        resolve_refs("$bad.id", saved)
    with pytest.raises(RunError, match="no step saved as 'later'"):
        resolve_refs("$later.id", saved)


def test_run_files_need_a_golden_and_a_named_wrong_run(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="needs a 'golden' run"):
        RunFile.model_validate({"task": "t", "runs": {"wrong_x": {"steps": [{"tool": "a"}]}}})
    with pytest.raises(ValidationError, match="but no earlier step saved"):
        run({"tool": "a", "args": {"id": "$nothing.id"}})
    (tmp_path / "hello-count-to-three.yaml").write_text(
        "task: hello-count-to-three\n"
        "runs:\n"
        "  golden: {steps: [{tool: finish}]}\n"
        "  wrong_stops: {fails_with: hello_world.wrong_count, steps: [{tool: finish}]}\n"
    )
    runs = load_runs("hello-count-to-three", tmp_path)
    assert runs.wrong == ["wrong_stops"]
    (tmp_path / "other.yaml").write_text((tmp_path / "hello-count-to-three.yaml").read_text())
    with pytest.raises(ValueError, match="is for task 'hello-count-to-three'"):
        load_runs("other", tmp_path)
