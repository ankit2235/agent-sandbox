"""``sandbox run``: resuming after a kill, retries, errors, concurrency, sampling, hidden sets."""

import threading
from pathlib import Path

import pytest
import yaml
from runner_testkit import call, read_json, scripted, turn, world_config, write_hidden_set

from kit.world import worlds
from runner.models import FakeModel, ProviderError, model_factory
from runner.runs import (
    MANIFEST,
    RESULT_FILE,
    RUNS_DIR,
    TRACE_FILE,
    ResumeMismatch,
    backoff,
    execute,
    load_records,
    read_manifest,
    resolve_task_set,
    select_tasks,
)


def files(out: Path) -> dict[str, bytes]:
    """Every result and trace in a run directory, by relative path."""
    return {
        str(path.relative_to(out)): path.read_bytes()
        for path in sorted((out / RUNS_DIR).rglob("*"))
        if path.is_file()
    }


class Killed(BaseException):
    """Stands in for the process being killed midway: not an Exception, nothing catches it."""


def killing_factory(after: int):
    """fake:golden that dies when the run after ``after`` finished runs starts."""
    golden = model_factory("fake:golden")
    started = 0
    lock = threading.Lock()

    def build(spec, task):
        nonlocal started
        with lock:
            started += 1
            if started > after:
                raise Killed
        return golden(spec, task)

    return build


def test_a_killed_run_resumes_to_exactly_the_uninterrupted_result(tmp_path) -> None:
    config = world_config("payments", runs=2, sample=4)
    clean = tmp_path / "clean"
    execute(config, clean)

    resumed = tmp_path / "resumed"
    with pytest.raises(Killed):
        execute(config, resumed, factory=killing_factory(after=3))
    manifest = read_manifest(resumed)
    assert manifest is not None
    assert manifest.status.completed == 3
    assert manifest.status.pending == 5

    # A run that was mid-way when the process died: a trace, no result.
    partial = resumed / RUNS_DIR / "fake-golden-partial"
    partial.mkdir()
    (partial / TRACE_FILE).write_text("half a trace")

    finished: list[str] = []
    execute(config, resumed, report=lambda record: finished.append(record.run_id))
    assert len(finished) == 5
    assert read_manifest(resumed).status.completed == 8
    expected = files(clean)
    got = {path: data for path, data in files(resumed).items() if "partial" not in path}
    assert got == expected


def test_resuming_with_different_settings_is_refused(tmp_path) -> None:
    out = tmp_path / "out"
    execute(world_config("payments", sample=2), out)
    with pytest.raises(ResumeMismatch, match="runs"):
        execute(world_config("payments", sample=2, runs=3), out)
    with pytest.raises(ResumeMismatch, match="sample"):
        execute(world_config("payments", sample=3), out)


def test_concurrency_changes_nothing_but_speed(tmp_path) -> None:
    config = world_config("security", sample=6, runs=2)
    execute(config, tmp_path / "one", concurrency=1)
    execute(config, tmp_path / "four", concurrency=4)
    assert files(tmp_path / "one") == files(tmp_path / "four")


def test_sampling_is_seeded() -> None:
    spec = worlds.get("payments")
    first = [task.id for task in select_tasks(spec, world_config("payments", sample=10))]
    again = [task.id for task in select_tasks(spec, world_config("payments", sample=10))]
    other = [t.id for t in select_tasks(spec, world_config("payments", sample=10, sample_seed=1))]
    assert first == again
    assert len(first) == 10
    assert first != other


def test_provider_errors_are_retried_with_backoff(tmp_path, hello_config) -> None:
    slept: list[float] = []
    hiccup = ProviderError("rate limited", retriable=True)
    execute(
        hello_config(),
        tmp_path / "out",
        factory=scripted(hiccup, hiccup, turn(call("increment", {"by": 3})), turn(text="ok")),
        sleep=slept.append,
    )
    [record] = load_records(tmp_path / "out")
    assert record.status == "completed"
    assert record.retries == 2
    assert slept == [2.0, 4.0]
    assert [backoff(n) for n in range(7)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_a_provider_that_keeps_failing_errors_the_run_and_resume_retries_it(
    tmp_path, hello_config
) -> None:
    out = tmp_path / "out"
    down = ProviderError("503", retriable=True)
    slept: list[float] = []
    manifest = execute(
        hello_config(retries=2), out, factory=scripted(down, down, down), sleep=slept.append
    )
    assert manifest.status.errored == 1
    assert read_json(out / MANIFEST)["status"]["errored"] == 1
    [record] = load_records(out)
    assert record.status == "error"
    assert "after 2 retries" in (record.error or "")
    assert record.grade is None
    assert slept == [2.0, 4.0]

    manifest = execute(hello_config(retries=2), out, factory=scripted(turn(text="back")))
    assert (manifest.status.completed, manifest.status.errored) == (1, 0)


def test_a_fatal_provider_error_stops_the_whole_command(tmp_path, hello_config) -> None:
    bad_key = ProviderError("AuthenticationError: bad key", retriable=False)
    with pytest.raises(ProviderError, match="bad key"):
        execute(hello_config(), tmp_path / "out", factory=scripted(bad_key))
    assert not list((tmp_path / "out" / RUNS_DIR).glob(f"*/{RESULT_FILE}"))


def test_an_unknown_price_cannot_run_under_a_cost_cap(tmp_path, hello_config) -> None:
    reply = turn(call("get_count"))
    reply.cost_known = False
    with pytest.raises(ProviderError, match="--max-cost cannot be enforced"):
        execute(hello_config(max_cost=1.0), tmp_path / "out", factory=scripted(reply))


def test_a_payments_tool_outside_the_task_is_an_unauthorised_action(tmp_path) -> None:
    spec = worlds.get("payments")
    task = spec.task_source().get("rails-gym-autopay-mandate")
    outside = "open_dispute"
    assert outside in spec.tool_names
    assert outside not in task.allowed_tools
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / f"{task.id}.yaml").write_text(yaml.safe_dump(task.model_dump(mode="json")))
    config = world_config("payments", model="fake:script", tasks=str(tasks))
    execute(config, tmp_path / "out", factory=scripted(turn(call(outside, {}))))
    [record] = load_records(tmp_path / "out")
    assert record.grade is not None
    assert record.grade.failure_type == "kit.unauthorised_action"


def test_task_sets_by_name_directory_or_hidden(tmp_path, monkeypatch) -> None:
    spec = worlds.get("security")
    assert len(resolve_task_set(spec, "security-v0.1")) == 50
    with pytest.raises(ValueError, match="unknown task set"):
        resolve_task_set(spec, "security-v9")
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    with pytest.raises(ValueError, match="needs HIDDEN_TASKS_DIR"):
        resolve_task_set(spec, "security-v0.1-hidden")
    hidden = write_hidden_set(tmp_path, "security", count=2)
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(hidden))
    assert [t.visibility for t in resolve_task_set(spec, "security-v0.1-hidden")] == ["hidden"] * 2


def test_a_scripted_model_sees_every_tool_result(tmp_path, hello_config) -> None:
    model = FakeModel([turn(call("get_count", call_id="g")), turn(text="done")])
    execute(hello_config(), tmp_path / "out", factory=lambda spec, task: model)
    tool = model.calls[1][-1]
    assert tool["tool_call_id"] == "g"
    assert '"count": 0' in tool["content"]
