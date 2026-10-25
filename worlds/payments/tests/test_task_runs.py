"""Every public task, played through its golden and wrong runs.

CONTRIBUTING rule 4 as a test: each task's golden run must pass its grader, and
every wrong run must fail it with the failure type it declares. A grader that
passes a known-bad run fails CI here.
"""

import json

import jsonschema
import pytest
import yaml

from kit.tasks import load_tasks
from kit.types import TaskSpec
from runner.cli import main
from worlds.payments import world_spec
from worlds.payments.graders.runs import GOLDEN, RUNS_DIR, load_runs, play
from worlds.payments.task_format import (
    AGENT_TASKS_DIR,
    TASK_SCHEMA_FILE,
    TASKS_DIR,
    schema_document,
)

TASKS = load_tasks(TASKS_DIR)
CASES = [
    pytest.param(task, name, id=f"{task.id}:{name}")
    for task in TASKS
    for name in load_runs(task.id).runs
]


@pytest.mark.parametrize(("task", "name"), CASES)
def test_golden_passes_and_wrong_runs_fail_as_declared(task: TaskSpec, name: str) -> None:
    run = load_runs(task.id).runs[name]
    result = play(task, run)
    grade = result.grade
    assert grade is not None
    report = "\n".join(f"{c.passed} {c.name}: {c.detail}" for c in grade.checks)
    if name == GOLDEN:
        assert grade.passed, f"golden run failed:\n{report}"
        assert grade.score == 1.0
    else:
        assert not grade.passed, f"grader passed a known-bad run:\n{report}"
        assert grade.failure_type == run.fails_with, report


def test_every_task_has_runs_and_every_run_file_has_a_task() -> None:
    """The run files of payments-v0.1-agents sit beside these; ``test_agents.py`` plays them."""
    task_ids = {task.id for task in TASKS} | {task.id for task in load_tasks(AGENT_TASKS_DIR)}
    run_ids = {path.stem for path in RUNS_DIR.glob("*.yaml")}
    assert task_ids == run_ids


def test_task_ids_are_unique_and_match_their_files() -> None:
    paths = sorted(TASKS_DIR.glob("**/*.yaml"))
    assert len({path.stem for path in paths}) == len(paths) == len(TASKS)
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["id"] == path.stem
        assert raw["category"] == path.parent.name


def test_every_task_file_matches_the_schema() -> None:
    schema = json.loads(TASK_SCHEMA_FILE.read_text(encoding="utf-8"))
    for path in sorted(TASKS_DIR.glob("**/*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(json.dumps(raw, default=str)), schema)


def test_the_reviewed_task_schema_file_matches_the_code() -> None:
    assert TASK_SCHEMA_FILE.read_text(encoding="utf-8") == schema_document(), (
        "the task file schema changed: review the change, then regenerate with\n"
        'uv run python -c "from worlds.payments.task_format import TASK_SCHEMA_FILE, '
        "schema_document; TASK_SCHEMA_FILE.write_text(schema_document(), encoding='utf-8')\""
    )


def test_the_world_ships_its_tasks() -> None:
    assert world_spec().tasks_dir == TASKS_DIR
    assert {task.id for task in world_spec().task_source().tasks} == {t.id for t in TASKS}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t.pop("reviewer_signoff"), "'reviewer_signoff' is a required property"),
        (lambda t: t.pop("expected_outcome"), "'expected_outcome' is a required property"),
        (lambda t: t["allowed_tools"].remove("ask_user"), "does not contain items"),
        (lambda t: t.update(grader="elsewhere:grader"), "does not match"),
        (lambda t: t["initial_state"].update(surprise=1), "Additional properties"),
    ],
)
def test_the_schema_rejects_an_incomplete_task(mutate, message: str) -> None:
    schema = json.loads(TASK_SCHEMA_FILE.read_text(encoding="utf-8"))
    raw = TASKS[0].model_dump(mode="json")
    mutate(raw)
    with pytest.raises(jsonschema.ValidationError, match=message):
        jsonschema.validate(raw, schema)


def test_the_same_run_twice_is_byte_identical() -> None:
    task = next(task for task in TASKS if task.id == "multi-mandate-buy-refund")
    run = load_runs(task.id).runs[GOLDEN]
    first, second = play(task, run), play(task, run)
    assert first.final_state == second.final_state
    assert first.trace.model_dump_json() == second.trace.model_dump_json()


def test_sandbox_tasks_validate_checks_files_against_the_schema(tmp_path, capsys) -> None:
    source = next(TASKS_DIR.glob("rails/*.yaml"))
    target = tmp_path / "rails" / source.name
    target.parent.mkdir()
    text = source.read_text(encoding="utf-8")
    target.write_text(text.replace("reviewer_signoff: null\n", ""), encoding="utf-8")
    assert main(["tasks", "validate", str(tmp_path)]) == 1
    assert "'reviewer_signoff' is a required property" in capsys.readouterr().out
    target.write_text(text, encoding="utf-8")
    assert main(["tasks", "validate", str(tmp_path)]) == 0
    assert "1 task(s) awaiting reviewer sign-off" in capsys.readouterr().out
