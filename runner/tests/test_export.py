"""Leaderboard export: the DATA FILES format, both worlds, and no hidden task ever published."""

import datetime as dt
import json
from pathlib import Path

import jsonschema
import pytest
from runner_testkit import read_json, world_config, write_hidden_set

from kit.trace import Trace
from kit.world import worlds
from runner.export import (
    WORLDS_FILE,
    export,
    task_title,
    trace_document,
    validate,
    world_entry,
)
from runner.runs import RunRecord, execute

SAMPLE = Path(__file__).parent / "fixtures" / "data_files_sample"
DAY = dt.date(2026, 10, 23)


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("worlds", "worlds.json"),
        ("results", "payments/results.json"),
        ("tasks", "payments/tasks.json"),
        ("trace", "payments/traces/run-a-rails-001.json"),
    ],
)
def test_the_leaderboards_own_example_matches_our_schemas(name: str, path: str) -> None:
    validate(name, read_json(SAMPLE / path))


def test_the_schemas_reject_documents_that_drift(tmp_path) -> None:
    results = read_json(SAMPLE / "payments/results.json")
    results["models"][0]["score"] = 1  # a field the leaderboard does not know
    with pytest.raises(jsonschema.ValidationError):
        validate("results", results)
    entry = read_json(SAMPLE / "worlds.json")[0] | {"status": "beta"}
    with pytest.raises(jsonschema.ValidationError):
        validate("worlds", [entry])


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> Path:
    """One export of three runs: payments golden (3 runs), payments wrong, security golden."""
    root = tmp_path_factory.mktemp("export")
    execute(world_config("payments", runs=3, sample=5), root / "pay-golden")
    execute(world_config("payments", model="fake:wrong", sample=5), root / "pay-wrong")
    execute(world_config("security", sample=5), root / "sec-golden")
    out = root / "data"
    export([root / "pay-golden", root / "pay-wrong", root / "sec-golden"], out, DAY)
    return out


def test_every_exported_file_is_valid_and_in_place(exported: Path) -> None:
    entries = read_json(exported / WORLDS_FILE)
    validate("worlds", entries)
    assert [entry["id"] for entry in entries] == ["payments", "security"]
    for world in ("payments", "security"):
        validate("results", read_json(exported / world / "results.json"))
        validate("tasks", read_json(exported / world / "tasks.json"))
        for path in (exported / world / "traces").glob("*.json"):
            validate("trace", read_json(path))


def test_results_carry_the_world_version_and_one_entry_per_model(exported: Path) -> None:
    results = read_json(exported / "payments/results.json")
    assert (results["world"], results["version"], results["updated"]) == (
        "payments", "payments-v0.1", "2026-10-23",
    )  # fmt: skip
    models = {model["id"]: model for model in results["models"]}
    assert sorted(models) == ["fake-golden", "fake-wrong"]
    golden, wrong = models["fake-golden"], models["fake-wrong"]
    assert golden["runs"] == 3
    assert len(golden["task_results"]) == 15
    assert golden["metrics"]["public"]["overall"] == {"value": 100.0, "ci": 0.0}
    assert wrong["metrics"]["public"]["overall"]["value"] == 0.0
    assert sum(wrong["failures"].values()) == 5
    assert "hidden" not in golden["metrics"]
    traces = {path.stem for path in (exported / "payments/traces").glob("*.json")}
    assert traces == {row["run_id"] for m in models.values() for row in m["task_results"]}


def test_tasks_json_has_titles_checks_and_pass_rates(exported: Path) -> None:
    tasks = read_json(exported / "payments/tasks.json")
    assert len(tasks) == 5
    for task in tasks:
        assert task["grader_checks"][-1] == "no_unauthorised_actions"
        assert task["pass_rate"] == 75.0  # 3 golden passes, 1 wrong failure


def test_worlds_json_comes_from_the_listing(exported: Path) -> None:
    security = next(e for e in read_json(exported / WORLDS_FILE) if e["id"] == "security")
    assert security["status"] == "preview"
    assert security["disclaimer"] == "Defensive scenarios only. All incidents are synthetic."
    labels = {metric["key"]: metric["label"] for metric in security["metrics"]}
    assert labels["safe_action_rate"] == "No-disruption rate"
    keys = [kind["key"] for kind in security["failure_types"]]
    assert "false_containment" in keys and "incomplete" in keys
    assert security["tools"] == worlds.get("security").tool_names


def test_other_worlds_in_worlds_json_are_kept(tmp_path, exported: Path) -> None:
    out = tmp_path / "data"
    out.mkdir()
    sample = read_json(SAMPLE / "worlds.json")
    (out / WORLDS_FILE).write_text(json.dumps(sample))
    run = tmp_path / "run"
    execute(world_config("security", sample=2), run)
    export([run], out, DAY)
    entries = read_json(out / WORLDS_FILE)
    assert [entry["id"] for entry in entries] == ["payments", "security", "drug-safety"]
    assert entries[0] == sample[0]  # payments was not exported, so it is untouched
    assert entries[1]["name"] == "Security operations"  # replaced from the listing


def test_stale_traces_are_removed_on_re_export(tmp_path) -> None:
    run = tmp_path / "run"
    execute(world_config("payments", sample=2), run)
    stale = tmp_path / "data/payments/traces/old-run.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    export([run], tmp_path / "data", DAY)
    assert not stale.exists()


def test_hidden_runs_only_reach_the_hidden_aggregate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "HIDDEN_TASKS_DIR", str(write_hidden_set(tmp_path, "payments", 3, monkeypatch))
    )
    public, hidden = tmp_path / "public", tmp_path / "hidden"
    # Hidden tasks have no run files in the public repo, so both runs use fake:idle.
    execute(world_config("payments", model="fake:idle", sample=3), public)
    execute(world_config("payments", model="fake:idle", tasks="payments-v0.1-hidden"), hidden)
    out = tmp_path / "data"
    export([public, hidden], out, DAY)
    [model] = read_json(out / "payments/results.json")["models"]
    assert set(model["metrics"]["hidden"]) == {"overall", "task_success_rate", "safe_action_rate"}
    published = "".join(path.read_text() for path in out.rglob("*.json"))
    assert "secret-" not in published
    assert len(model["task_results"]) == 3


def test_a_world_without_a_listing_is_not_exported(tmp_path, hello_config) -> None:
    from runner_testkit import scripted, turn

    run = tmp_path / "run"
    execute(hello_config(), run, factory=scripted(turn(text="stop")))
    with pytest.raises(ValueError, match="no leaderboard listing"):
        export([run], tmp_path / "data", DAY)
    with pytest.raises(ValueError, match="no leaderboard listing"):
        world_entry(worlds.get("hello_world"))


def test_hidden_only_runs_cannot_make_a_public_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "HIDDEN_TASKS_DIR", str(write_hidden_set(tmp_path, "security", 1, monkeypatch))
    )
    run = tmp_path / "hidden"
    execute(world_config("security", model="fake:idle", tasks="security-v0.1-hidden"), run)
    with pytest.raises(ValueError, match="no completed public runs"):
        export([run], tmp_path / "data", DAY)


def test_the_model_text_rides_on_the_first_call_of_its_turn(tmp_path, hello_config) -> None:
    from runner_testkit import call, scripted, turn

    run = tmp_path / "run"
    execute(
        hello_config(),
        run,
        factory=scripted(
            turn(call("increment", {"by": 2}), call("increment", {"by": 1}), text="Counting."),
            turn(call("increment", "{bad"), text="Oops."),
            turn(call("finish"), text="Finishing."),
        ),
    )
    record = RunRecord.model_validate_json(
        next(run.glob("runs/*/result.json")).read_text(encoding="utf-8")
    )
    trace = Trace.read_jsonl(run / "runs" / record.run_id / "trace.jsonl")
    steps = trace_document(record, trace, "scripted")["steps"]
    assert [(s["n"], s["tool"], s["agent_message"]) for s in steps] == [
        (1, "increment", "Counting."),
        (2, "increment", None),
        (3, None, "Oops."),
        (4, "finish", "Finishing."),
    ]


def test_titles_are_made_from_ids() -> None:
    spec = worlds.get("payments")
    task = spec.task_source().get("rails-gym-autopay-mandate")
    assert task_title(task) == "Gym autopay mandate"
