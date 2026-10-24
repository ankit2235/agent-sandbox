"""Every public task, played through its golden and wrong runs (CONTRIBUTING rule 4)."""

import json

import jsonschema
import pytest
import yaml
from security_testkit import TASKS, Session

from kit.types import TaskSpec
from runner.cli import main
from worlds.security import world_spec
from worlds.security.graders.runs import GOLDEN, RUNS_DIR, load_runs, play
from worlds.security.task_format import TASK_SCHEMA_FILE, TASKS_DIR, schema_document

CASES = [
    pytest.param(task, name, id=f"{task.id}:{name}")
    for task in TASKS
    for name in load_runs(task.id).runs
]


@pytest.mark.parametrize(("task", "name"), CASES)
def test_golden_passes_and_wrong_runs_fail_as_declared(task: TaskSpec, name: str) -> None:
    run = load_runs(task.id).runs[name]
    grade = play(task, run).grade
    assert grade is not None
    report = "\n".join(f"{c.passed} {c.name}: {c.detail}" for c in grade.checks)
    if name == GOLDEN:
        assert grade.passed, f"golden run failed:\n{report}"
        assert grade.score == 1.0
    else:
        assert not grade.passed, f"grader passed a known-bad run:\n{report}"
        assert grade.failure_type == run.fails_with, report


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_no_sla_has_run_out_when_the_agent_starts(task: TaskSpec) -> None:
    """Unless the story says a ticket is meant to start breached (``breached_at_load``)."""
    session = Session(task)
    story = {ticket.id: ticket for ticket in world_spec_story(task).tickets}
    for ticket in session.admin("tickets", "tickets"):
        intended = story[ticket["id"]].breached_at_load
        assert ticket["sla_breached"] is intended, ticket
        settled = ticket["escalated_at"] is not None or ticket["status"] == "closed"
        if not settled and not intended:
            assert ticket["sla_due_at"] > "2026-01-01T09:00:00Z", ticket


def world_spec_story(task: TaskSpec):
    from worlds.security.state import story_of

    return story_of(task)


def test_every_task_has_runs_and_every_run_file_has_a_task() -> None:
    assert {task.id for task in TASKS} == {path.stem for path in RUNS_DIR.glob("*.yaml")}


def test_task_ids_are_unique_and_match_their_files() -> None:
    paths = sorted(TASKS_DIR.glob("**/*.yaml"))
    assert len({path.stem for path in paths}) == len(paths) == len(TASKS)
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["id"] == path.stem
        assert raw["category"] == path.parent.name
        assert raw["reviewer_signoff"] is None, "only a named SOC reviewer signs off"


def test_every_task_file_matches_the_schema() -> None:
    schema = json.loads(TASK_SCHEMA_FILE.read_text(encoding="utf-8"))
    for path in sorted(TASKS_DIR.glob("**/*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(json.dumps(raw, default=str)), schema)


def test_the_reviewed_task_schema_file_matches_the_code() -> None:
    assert TASK_SCHEMA_FILE.read_text(encoding="utf-8") == schema_document(), (
        "the task file schema changed: review the change, then regenerate with\n"
        'uv run python -c "from worlds.security.task_format import TASK_SCHEMA_FILE, '
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
    task = next(task for task in TASKS if task.id == "multi-maintenance-vs-lateral")
    run = load_runs(task.id).runs[GOLDEN]
    first, second = play(task, run), play(task, run)
    assert first.final_state == second.final_state
    assert first.trace.model_dump_json() == second.trace.model_dump_json()


def test_sandbox_tasks_validate_checks_security_files_against_the_schema(tmp_path, capsys) -> None:
    source = next(TASKS_DIR.glob("triage/*.yaml"))
    target = tmp_path / "triage" / source.name
    target.parent.mkdir()
    text = source.read_text(encoding="utf-8")
    target.write_text(text.replace("reviewer_signoff: null\n", ""), encoding="utf-8")
    assert main(["tasks", "validate", str(tmp_path)]) == 1
    assert "'reviewer_signoff' is a required property" in capsys.readouterr().out
    target.write_text(text, encoding="utf-8")
    assert main(["tasks", "validate", str(tmp_path)]) == 0
    assert "1 task(s) awaiting reviewer sign-off" in capsys.readouterr().out
