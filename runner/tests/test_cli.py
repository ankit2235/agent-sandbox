import json

import pytest

from runner.cli import main


def test_worlds_lists_the_installed_worlds(capsys) -> None:
    assert main(["worlds"]) == 0
    out = capsys.readouterr().out
    assert "hello_world" in out
    assert "get_count" in out


def test_worlds_json_is_machine_readable(capsys) -> None:
    assert main(["worlds", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(entry["name"] == "hello_world" for entry in payload)


def test_tasks_validate_passes_for_the_packaged_tasks(capsys) -> None:
    assert main(["tasks", "validate"]) == 0
    out = capsys.readouterr().out
    assert "task files valid" in out
    assert "FAIL" not in out


def test_tasks_validate_reports_a_broken_task(tmp_path, capsys) -> None:
    (tmp_path / "broken.yaml").write_text(
        "\n".join(
            [
                "id: broken-task",
                "version: v0.1",
                "world: hello_world",
                "category: basics",
                "difficulty: easy",
                "instruction: Count wrong.",
                "initial_state: {start: 0}",
                "allowed_tools: [get_count]",
                "max_steps: 3",
                "grader: kit.examples.hello_world.graders:reaches_target",
            ]
        )
    )
    assert main(["tasks", "validate", str(tmp_path)]) == 1
    assert "FAIL hello_world/v0.1/broken-task" in capsys.readouterr().out


def test_example_runs_the_golden_policy(capsys) -> None:
    assert main(["example"]) == 0
    out = capsys.readouterr().out
    assert "PASS counter_on_target" in out
    assert "score 1.00" in out


@pytest.mark.parametrize(
    "policy",
    [
        "kit.examples.hello_world.scripted:wrong_run_overshoots",
        "kit.examples.hello_world.scripted:wrong_run_never_finishes",
    ],
)
def test_example_reports_a_failing_policy(policy: str, capsys) -> None:
    assert main(["example", "--policy", policy]) == 2
    assert "FAIL" in capsys.readouterr().out


def test_example_can_write_a_trace(tmp_path, capsys) -> None:
    destination = tmp_path / "run.jsonl"
    assert main(["example", "--trace", str(destination)]) == 0
    lines = destination.read_text().splitlines()
    assert json.loads(lines[0])["world"] == "hello_world"
    assert len(lines) > 1
