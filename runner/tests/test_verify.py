"""``sandbox verify``, and every task of both worlds through the whole runner.

The full-world tests are the CI stand-in for a model run: ``fake:golden`` must
pass all 50 tasks of each world through MCP, ``fake:wrong`` must fail each one
with the failure type its run file declares, and ``verify`` must reproduce
every grade from the recorded traces.
"""

import json
from pathlib import Path

import pytest
from runner_testkit import call, scripted, turn, world_config

from kit.scripted import load_runs
from kit.tasks import load_task
from kit.world import worlds
from runner.runs import RESULT_FILE, RUNS_DIR, TRACE_FILE, execute, load_records
from runner.verify import verify


@pytest.mark.parametrize("world", ["payments", "security"])
def test_every_golden_run_passes_through_the_runner_and_reproduces(tmp_path, world: str) -> None:
    out = tmp_path / world
    manifest = execute(world_config(world), out, concurrency=4)
    assert (manifest.status.total, manifest.status.completed) == (50, 50)
    records = load_records(out)
    failed = [record.run_id for record in records if not record.passed]
    assert not failed
    report = verify(out)
    assert report.ok
    assert len(report.verdicts) == 50


@pytest.mark.parametrize("world", ["payments", "security"])
def test_every_wrong_run_fails_with_its_declared_type_and_reproduces(tmp_path, world: str) -> None:
    spec = worlds.get(world)
    assert spec.runs_dir is not None
    out = tmp_path / world
    execute(world_config(world, model="fake:wrong"), out, concurrency=4)
    for record in load_records(out):
        runs = load_runs(record.task_id, spec.runs_dir)
        assert record.grade is not None
        assert record.grade.failure_type == runs.runs[runs.wrong[0]].fails_with, record.run_id
    assert verify(out).ok


@pytest.fixture
def recorded(tmp_path) -> Path:
    out = tmp_path / "run"
    execute(world_config("payments", sample=3, runs=2), out)
    return out


def test_a_tampered_tool_response_is_caught(recorded: Path) -> None:
    target = next((recorded / RUNS_DIR).iterdir()) / TRACE_FILE
    lines = target.read_text().splitlines()
    for number, line in enumerate(lines[1:], start=1):
        step = json.loads(line)
        if step["kind"] == "tool_call" and isinstance(step["response"], dict):
            step["response"]["text"] = "something else happened"
            lines[number] = json.dumps(step)
            break
    target.write_text("\n".join(lines) + "\n")
    report = verify(recorded)
    assert not report.ok
    [bad] = [verdict for verdict in report.verdicts if not verdict.ok]
    assert "world steps differ" in bad.problems[0]


def test_a_tampered_grade_is_caught(recorded: Path) -> None:
    target = next((recorded / RUNS_DIR).iterdir()) / RESULT_FILE
    record = json.loads(target.read_text())
    record["grade"]["passed"] = not record["grade"]["passed"]
    target.write_text(json.dumps(record))
    problems = [p for verdict in verify(recorded).verdicts for p in verdict.problems]
    assert problems and problems[0].startswith("grade differs")


def test_a_dropped_action_is_caught(recorded: Path) -> None:
    target = next((recorded / RUNS_DIR).iterdir()) / TRACE_FILE
    lines = target.read_text().splitlines()
    calls = [n for n, line in enumerate(lines) if '"kind":"tool_call"' in line.replace(" ", "")]
    del lines[calls[0]]
    target.write_text("\n".join(lines) + "\n")
    assert not verify(recorded).ok


def test_errored_runs_are_skipped_and_a_changed_task_is_reported(tmp_path, hello_config, hello_dir):
    from runner.models import ProviderError

    errored = tmp_path / "errored"
    down = ProviderError("503", retriable=True)
    execute(hello_config(retries=0), errored, factory=scripted(down), sleep=lambda _: None)
    report = verify(errored)
    assert report.skipped == ["fake-script-hello-count-to-three-r1"]
    assert report.verdicts == []

    fine = tmp_path / "fine"
    execute(hello_config(), fine, factory=scripted(turn(call("increment", {"by": 3}))))
    assert verify(fine).ok
    task = hello_dir / "hello-count-to-three.yaml"
    task.write_text(task.read_text().replace("max_steps: 8", "max_steps: 9"))
    [verdict] = verify(fine).verdicts
    assert "changed since the run" in verdict.problems[0]


def test_a_run_made_before_sign_off_still_reproduces_after_it(tmp_path, hello_config, hello_dir):
    out = tmp_path / "before"
    execute(hello_config(), out, factory=scripted(turn(call("increment", {"by": 3}))))
    task = hello_dir / "hello-count-to-three.yaml"
    signed = "reviewer_signoff:\n  name: A. Reviewer\n  role: example\n  date: 2026-11-01\n"
    text = task.read_text()
    task.write_text(
        text.replace("reviewer_signoff: null\n", signed)
        if "reviewer_signoff:" in text
        else text + signed
    )
    assert load_task(task).reviewer_signoff is not None
    assert verify(out).ok
