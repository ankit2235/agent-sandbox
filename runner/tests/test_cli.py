import json
from pathlib import Path

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
    assert f"FAIL {tmp_path / 'broken.yaml'}" in capsys.readouterr().out


HELLO = Path(__file__).parents[2] / "kit/src/kit/examples/hello_world/tasks"


def copy_hello(destination: Path, name: str = "hello-count-to-three.yaml") -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    target.write_text((HELLO / "hello-count-to-three.yaml").read_text())
    return target


def test_tasks_validate_wants_the_file_named_after_the_task(tmp_path, capsys) -> None:
    copy_hello(tmp_path, "renamed.yaml")
    assert main(["tasks", "validate", str(tmp_path)]) == 1
    assert "does not match the task id 'hello-count-to-three'" in capsys.readouterr().out


def test_tasks_validate_wants_a_task_in_its_category_directory(tmp_path, capsys) -> None:
    copy_hello(tmp_path / "basics")
    assert main(["tasks", "validate", str(tmp_path)]) == 0
    copy_hello(tmp_path / "elsewhere")
    assert main(["tasks", "validate", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "must live in a basics/ directory" in out


def test_tasks_validate_rejects_duplicate_ids(tmp_path, capsys) -> None:
    copy_hello(tmp_path / "basics")
    copy_hello(tmp_path)
    assert main(["tasks", "validate", str(tmp_path)]) == 1
    assert "duplicate task id" in capsys.readouterr().out


def test_tasks_validate_counts_tasks_awaiting_sign_off(tmp_path, capsys) -> None:
    target = copy_hello(tmp_path)
    assert main(["tasks", "validate", str(tmp_path)]) == 0
    assert "awaiting reviewer sign-off" not in capsys.readouterr().out
    text = target.read_text()
    target.write_text(text[: text.index("reviewer_signoff:")] + "reviewer_signoff: null\n")
    assert main(["tasks", "validate", str(tmp_path)]) == 0
    assert "1 task(s) awaiting reviewer sign-off" in capsys.readouterr().out


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


def test_run_verify_and_export_from_the_command_line(tmp_path, capsys) -> None:
    out = tmp_path / "run"
    args = ["run", "--world", "security", "--model", "fake:golden", "--tasks", "security-v0.1"]
    args += ["--sample", "3", "--runs", "2", "--concurrency", "2", "--out", str(out)]
    assert main([*args, "--updated", "2026-10-23"]) == 0
    printed = capsys.readouterr().out
    assert "6/6 runs completed, 0 errored, 0 pending" in printed
    assert "public: overall 100.0%" in printed
    assert (out / "export" / "security" / "results.json").exists()

    assert main(args) == 0  # resuming a finished directory runs nothing
    assert "pass " not in capsys.readouterr().out

    assert main(["verify", "--run", str(out)]) == 0
    assert "6/6 runs reproduced their grades" in capsys.readouterr().out

    data = tmp_path / "data"
    assert main(["export", str(out), "--out", str(data), "--updated", "2026-10-23"]) == 0
    results = json.loads((data / "security" / "results.json").read_text())
    assert results["updated"] == "2026-10-23"


def test_export_needs_a_date_and_a_real_one(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["export", str(tmp_path), "--out", str(tmp_path / "data")])
    with pytest.raises(SystemExit):
        main(["export", str(tmp_path), "--out", str(tmp_path / "d"), "--updated", "23/10/2026"])
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_verify_fails_on_a_directory_with_nothing_to_check(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="not a run directory"):
        main(["verify", "--run", str(tmp_path)])
