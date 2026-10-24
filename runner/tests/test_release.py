"""``sandbox release-check`` and ``sandbox freeze``: every condition, passing and failing.

The fast cases use hello_world (two tasks); the full gate runs once on payments, where
today the people- and model-dependent conditions fail by name.
"""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from runner_testkit import hello_result

from kit.types import Signoff
from kit.world import worlds
from runner import hidden, release
from runner.cli import main
from runner.export import build_world, load_run_dir
from runner.runs import RESULT_FILE, RUNS_DIR, write_json

ROOT = Path(__file__).parents[2]
NAMES = [
    "public-tasks",
    "hidden-set",
    "signoff",
    "grader-runs",
    "frozen",
    "calibration",
    "results",
    "published",
    "no-hidden",
]


def test_the_gate_names_every_condition_and_what_it_waits_on(monkeypatch, capsys) -> None:
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    assert main(["release-check", "payments-v0.1", "--repo", str(ROOT)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "release-check payments-v0.1"
    marks = {line.split()[1]: line for line in lines[1:10]}
    assert list(marks) == NAMES
    for name in ("public-tasks", "grader-runs", "frozen", "no-hidden"):
        assert marks[name].startswith("PASS"), marks[name]
    assert marks["hidden-set"].endswith("[waits on: HIDDEN_TASKS_DIR]")
    assert marks["signoff"].endswith("[waits on: reviewer]")
    for name in ("calibration", "results", "published"):
        assert marks[name].endswith("[waits on: real model runs]")
    assert lines[-1].startswith("4 of 9 conditions pass; failing: hidden-set, signoff")


def test_an_unknown_task_set_is_refused() -> None:
    with pytest.raises(ValueError, match="no installed world has the task set 'payments-v9'"):
        release.world_of("payments-v9")
    assert release.world_of("security-v0.1").name == "security"


def test_the_committed_freeze_file_matches_the_packaged_tasks() -> None:
    assert release.freeze_problems(worlds.get("payments")) == []


def test_a_freeze_file_reports_every_change(tmp_path, monkeypatch) -> None:
    spec = worlds.get("payments")
    path = tmp_path / "frozen-v0.1.json"
    monkeypatch.setattr(release, "freeze_file", lambda s: path)
    assert "no freeze file" in release.freeze_problems(spec)[0]
    release.write_freeze(spec)
    assert release.freeze_problems(spec) == []
    data = json.loads(path.read_text())
    first, second = sorted(data["tasks"])[:2]
    data["tasks"][first] = "0" * 64
    del data["tasks"][second]
    data["tasks"]["rails-not-a-task"] = "1" * 64
    path.write_text(json.dumps(data))
    assert release.freeze_problems(spec) == [
        f"{second} was added",
        "rails-not-a-task was removed",
        f"{first} changed",
    ]
    path.write_text(json.dumps({"format": "other"}))
    assert "is not a frozen-tasks-v1 list" in release.freeze_problems(spec)[0]


def test_a_sign_off_changes_neither_the_freeze_nor_a_run(monkeypatch) -> None:
    spec = worlds.get("payments")
    before = release.frozen_list(spec)
    signed = [
        task.model_copy(
            update={"reviewer_signoff": Signoff(name="A. Reviewer", role="ops", date="2026-11-01")}
        )
        for task in release.public_tasks(spec)
    ]
    monkeypatch.setattr(release, "public_tasks", lambda s: signed)
    assert release.frozen_list(spec) == before
    assert release.check_signoff(spec).passed
    unsigned_last = [*signed[:-1], signed[-1].model_copy(update={"reviewer_signoff": None})]
    monkeypatch.setattr(release, "public_tasks", lambda s: unsigned_last)
    condition = release.check_signoff(spec)
    assert (condition.passed, condition.waits_on) == (False, "reviewer")
    assert condition.detail == "1 of 50 public tasks have no reviewer_signoff"


def test_too_few_tasks_and_no_run_files_fail_by_name() -> None:
    spec = worlds.get("hello_world")
    tasks = release.check_public_tasks(spec)
    assert not tasks.passed
    assert tasks.detail.startswith("2 public tasks, 50-80 needed")
    assert release.check_grader_runs(spec).detail == "the world ships no run files"


def test_the_hidden_set_needs_hidden_tasks_dir(monkeypatch) -> None:
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    condition = release.check_hidden_set(worlds.get("payments"))
    assert (condition.passed, condition.detail) == (False, "HIDDEN_TASKS_DIR is not set")


@pytest.fixture(scope="module")
def private_set(tmp_path_factory):
    root = tmp_path_factory.mktemp("private") / "hidden-repo"
    hidden.generate(worlds.get("payments"), 3, 41, root)
    return root


def publish(private_set, tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(private_set))
    published = tmp_path / "hidden-v0.1.json"
    original = hidden.hash_file
    monkeypatch.setattr(
        hidden, "hash_file", lambda s: published if s.name == "payments" else original(s)
    )
    hidden.write_hashes(worlds.get("payments"), private_set)
    return published


def test_a_matching_hidden_set_passes_with_counts_only(private_set, tmp_path, monkeypatch) -> None:
    publish(private_set, tmp_path, monkeypatch)
    condition = release.check_hidden_set(worlds.get("payments"))
    assert condition.passed, condition.detail
    assert condition.detail.startswith("3 tasks, ")
    assert "hashes match the published list" in condition.detail
    for task in hidden.load_hidden(worlds.get("payments")):
        assert task.id not in condition.detail
        assert task.instruction.strip()[:30] not in condition.detail


def test_a_tampered_hash_list_fails_the_hidden_set(private_set, tmp_path, monkeypatch) -> None:
    published = publish(private_set, tmp_path, monkeypatch)
    data = json.loads(published.read_text())
    data["tasks"][0] = "f" * 64
    published.write_text(json.dumps(data))
    condition = release.check_hidden_set(worlds.get("payments"))
    assert not condition.passed
    assert "inconsistent" in condition.detail


def calibration_report(spec_name: str, models: list[str], modifiers: list[str]) -> dict:
    return {
        "format": "stress-calibration-v1",
        "world": spec_name,
        "version": f"{spec_name}-v0.1",
        "models": models,
        "modifiers": [{"name": name, "tasks": 3, "results": {}} for name in modifiers],
        "best_stressed": 40.0,
        "best_under_target": True,
    }


def test_the_calibration_report_must_be_real_and_complete(tmp_path) -> None:
    spec = worlds.get("payments")
    path = tmp_path / "report.json"
    assert "no calibration report" in release.check_calibration(spec, path).detail
    path.write_text("{not json")
    assert "is not JSON" in release.check_calibration(spec, path).detail
    path.write_text(json.dumps({"format": "other"}))
    assert "is not a stress-calibration-v1 report" in release.check_calibration(spec, path).detail
    from stress.core import registry

    every = registry.get("payments").names()
    path.write_text(json.dumps(calibration_report("payments", ["fake:golden", "a/b"], every[1:])))
    detail = release.check_calibration(spec, path).detail
    assert "1 real model(s) of 2" in detail
    assert f"no row for {every[0]}" in detail
    path.write_text(json.dumps(calibration_report("payments", ["a/one", "b/two"], every)))
    condition = release.check_calibration(spec, path)
    assert condition.passed, condition.detail
    assert condition.detail.startswith(f"2 models, {len(every)} modifiers")


@pytest.fixture
def results(tmp_path) -> Path:
    root = tmp_path / "results"
    for model in ("test/model-a", "test/model-b"):
        hello_result(root / model.replace("/", "-"), model=model, runs=2)
    return root


def test_results_need_a_directory_and_enough_models(results, tmp_path) -> None:
    spec = worlds.get("hello_world")
    assert release.check_results(spec, None).condition.detail == "no --results directory given"
    missing = release.check_results(spec, tmp_path / "nowhere").condition
    assert "is not a directory" in missing.detail
    few = release.check_results(spec, results, min_models=3, min_runs=2).condition
    assert not few.passed
    assert few.detail == "2 of 3 models complete and reproduced"
    enough = release.check_results(spec, results, min_models=2, min_runs=2)
    assert enough.condition.passed, enough.condition.detail
    assert "test-model-a, test-model-b (8 runs, every one reproduced)" in enough.condition.detail


def test_one_tampered_result_fails_the_results(results) -> None:
    spec = worlds.get("hello_world")
    target = sorted((results / "test-model-b" / RUNS_DIR).iterdir())[0] / RESULT_FILE
    record = json.loads(target.read_text())
    record["grade"]["score"] = 1.0 - record["grade"]["score"]
    target.write_text(json.dumps(record))
    condition = release.check_results(spec, results, min_models=2, min_runs=2).condition
    assert not condition.passed
    assert "test-model-b: verify" in condition.detail
    assert "grade differs" in condition.detail


def test_runs_of_other_sets_are_not_counted(results, tmp_path) -> None:
    spec = worlds.get("hello_world")
    hello_result(results / "sampled", model="test/model-c", sample=1)
    condition = release.check_results(spec, results, min_models=2, min_runs=1).condition
    assert not condition.passed
    assert "sampled: ran a sample of 1 tasks" in condition.detail


def publish_results(results: Path, data: Path) -> None:
    """``results.json`` as ``sandbox export`` writes it (hello_world has no listing to export)."""
    spec = worlds.get("hello_world")
    run_dirs = [load_run_dir(path) for path in sorted(results.iterdir())]
    documents = build_world(spec, run_dirs, dt.date(2026, 11, 2))
    write_json(data / "hello_world" / "results.json", documents.results)


def test_the_published_files_must_be_what_the_results_export(results, tmp_path) -> None:
    spec = worlds.get("hello_world")
    found = release.check_results(spec, results, min_models=2, min_runs=2)
    data = tmp_path / "data"
    assert "no leaderboard file" in release.check_published(spec, data, found).detail
    publish_results(results, data)
    condition = release.check_published(spec, data, found)
    assert condition.passed, condition.detail
    path = data / "hello_world" / "results.json"
    published = json.loads(path.read_text())
    published["models"][0]["metrics"]["public"]["overall"]["value"] = 99.9
    path.write_text(json.dumps(published))
    condition = release.check_published(spec, data, found)
    assert not condition.passed
    assert "numbers differ" in condition.detail
    published["models"].pop()
    path.write_text(json.dumps(published))
    assert "models ['test-model-b'] differ" in release.check_published(spec, data, found).detail


def git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "scripts").mkdir()
    (path / release.LEAK_SCRIPT).write_text((ROOT / release.LEAK_SCRIPT).read_text())
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    return path


def test_no_hidden_finds_a_canary_and_a_task_marked_hidden(tmp_path) -> None:
    clean = git_repo(tmp_path / "clean", {"docs/a.md": "nothing here"})
    assert release.check_no_hidden(clean).passed
    canary = hidden.canary(5, "hidden-002")
    dirty = git_repo(
        tmp_path / "dirty",
        {"docs/a.md": f"see {canary}", "worlds/x/task.yaml": "id: x\nvisibility: hidden\n"},
    )
    condition = release.check_no_hidden(dirty)
    assert not condition.passed
    assert "contains a canary" in condition.detail
    assert "contains a task marked hidden" in condition.detail
    assert not release.check_no_hidden(tmp_path).passed


def test_freeze_from_the_command_line(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "frozen-v0.1.json"
    monkeypatch.setattr(release, "freeze_file", lambda s: path)
    assert main(["freeze", "payments-v0.1"]) == 0
    assert capsys.readouterr().out.startswith("50 tasks of payments-v0.1 frozen in")
    assert json.loads(path.read_text())["count"] == 50


def test_the_whole_gate_passes_when_every_condition_does(results, tmp_path, monkeypatch) -> None:
    """hello_world with every slow or people-dependent part stood in for."""
    data = tmp_path / "data"
    publish_results(results, data)
    repo = git_repo(tmp_path / "repo", {"README.md": "clean"})
    passing = release.Condition("x", True, "stood in")
    for name in ("check_public_tasks", "check_hidden_set", "check_grader_runs", "check_frozen"):
        monkeypatch.setattr(release, name, lambda spec, *a, **k: passing)
    monkeypatch.setattr(release, "check_calibration", lambda spec, path: passing)
    report = release.release_check(
        "hello_world-v0.1",
        repo=repo,
        results=results,
        published=data,
        min_models=2,
        min_runs=2,
    )
    assert report.ok, release.report_lines(report)
    assert release.report_lines(report)[-1] == "9 of 9 conditions pass; ready to release"


def test_quickstart_runs_ten_tasks_and_prints_a_score(tmp_path, capsys) -> None:
    out = tmp_path / "quick"
    assert main(["quickstart", "--model", "fake:golden", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "score: 100.0% ± 0.0 on 10 tasks of payments-v0.1 (fake:golden)" in printed
    assert len(list((out / RUNS_DIR).iterdir())) == 10


def test_quickstart_without_litellm_says_how_to_install_it(capsys) -> None:
    try:
        import litellm  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("litellm is installed here")
    assert main(["quickstart", "--model", "openai/some-model"]) == 2
    assert "pip install 'agent-sandbox-bench[litellm]'" in capsys.readouterr().out


def test_the_spot_check_from_the_command_line(tmp_path, capsys) -> None:
    from runner.runs import RunConfig, execute

    out = tmp_path / "idle"
    config = RunConfig(
        world="hello_world",
        tasks="hello_world-v0.1",
        model="fake:idle",
        model_name="idle",
        provider="fake",
    )
    execute(config, out)
    assert main(["submission", "spot-check", str(out), "--out", str(tmp_path / "re")]) == 0
    assert "ok " in capsys.readouterr().out
