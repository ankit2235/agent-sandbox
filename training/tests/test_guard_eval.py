"""The Guard models track: backends scored behind the guard on the dataset, guarded sandbox
runs in ``guards/results.json``, and the rules-only guard's overhead."""

import datetime as dt
import json
import os
import platform
import time
from pathlib import Path

import jsonschema
import pytest

from guard.backends.rules_only import RulesOnlyBackend
from guard.guarded_backend import GuardedBackend
from guard.policy import load_policy
from guard.records import DecisionLog, read_log, verify_chain
from runner.guarding import GuardSetup
from runner.runs import RunConfig, execute
from training.cli import main as train_cli
from training.data.build import read_split
from training.eval.report import SCHEMA, guard_runs, leaderboard

WARM_UP = 300
SAMPLE = 3000
LIMIT_MS = 5.0


def test_eval_behind_the_guard_writes_a_verifiable_log(small_build: Path, tmp_path: Path) -> None:
    out, log = tmp_path / "g.json", tmp_path / "g.decisions.jsonl"
    code = train_cli(
        [
            "eval",
            "--backend",
            "rules_only",
            "--guard",
            "{world}/default",
            "--data",
            str(small_build),
            "--out",
            str(out),
            "--decisions",
            str(log),
        ]
    )
    assert code == 0
    result = json.loads(out.read_text())
    assert result["set"] == "test+guard"
    assert set(result["guard"]) == {"payments", "security"}
    rows, problems = read_log(log)
    assert problems == [] and verify_chain(rows) == []
    assert len(rows) == result["rows"]


def test_the_guard_never_stops_fewer_mistakes_than_its_backend_alone(small_build: Path) -> None:
    rows = read_split(small_build, "test")
    policies = {world: load_policy(f"{world}/default") for world in ("payments", "security")}
    guarded = GuardedBackend(policies, RulesOnlyBackend())
    bare = RulesOnlyBackend()
    order = {"allow": 0, "ask_user": 1, "block": 2}
    for row in rows:
        alone = bare.decide(row.request).decision.decision
        behind = guarded.decide(row.request).decision.decision
        assert order[behind] >= order[alone], row.id


def test_hidden_decisions_are_never_written(small_build: Path, tmp_path: Path, capsys) -> None:
    code = train_cli(
        [
            "eval",
            "--backend",
            "rules_only",
            "--guard",
            "{world}/default",
            "--hidden",
            "--data",
            str(small_build),
            "--out",
            str(tmp_path / "h.json"),
            "--decisions",
            str(tmp_path / "h.jsonl"),
        ]
    )
    assert code == 2
    assert not (tmp_path / "h.jsonl").exists()
    assert "aggregates only" in capsys.readouterr().out


def _guarded_run(out: Path, model: str, *, guard: bool = True) -> Path:
    setup = GuardSetup(
        load_policy("payments/default"),
        RulesOnlyBackend(),
        policy_source="payments/default",
        backend_config={"kind": "rules_only", "name": "rules_only"},
    )
    config = RunConfig(
        world="payments",
        tasks="payments-v0.1",
        model=model,
        model_name=model,
        provider="fake",
        sample=5,
        guard=setup.config if guard else None,
    )
    execute(config, out, guard=setup if guard else None)
    return out


def test_guarded_runs_go_in_the_leaderboard_with_task_success_beside_block_rates(
    tmp_path: Path,
) -> None:
    golden = _guarded_run(tmp_path / "g", "fake:golden")
    wrong = _guarded_run(tmp_path / "w", "fake:wrong")
    plain = _guarded_run(tmp_path / "p", "fake:wrong", guard=False)
    runs = guard_runs([golden, wrong], [plain])
    data = leaderboard([], dt.date(2026, 11, 1), runs)
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    cells = data["backends"][0]["with_guard"]
    assert [(c["agent"], c["records_verified"]) for c in cells] == [
        ("fake:golden", True),
        ("fake:wrong", True),
    ]
    wrong_cell = cells[1]
    assert wrong_cell["task_success_without_guard"] is not None
    assert cells[0]["task_success_without_guard"] is None
    for cell in cells:
        assert 0 <= cell["task_success"]["value"] <= 1 and cell["block_rate"] is not None
    blocks_without_success = json.loads(json.dumps(data))
    del blocks_without_success["backends"][0]["with_guard"][0]["task_success"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(blocks_without_success, json.loads(SCHEMA.read_text()))


def test_the_report_refuses_what_is_not_a_guarded_run(tmp_path: Path, capsys) -> None:
    plain = _guarded_run(tmp_path / "p", "fake:golden", guard=False)
    golden = _guarded_run(tmp_path / "g", "fake:golden")
    out = tmp_path / "report"
    assert (
        train_cli(["report", "--runs", str(plain), "--out", str(out), "--updated", "2026-11-01"])
        == 2
    )
    assert "not a guarded run" in capsys.readouterr().out
    assert (
        train_cli(
            [
                "report",
                "--runs",
                str(golden),
                str(golden),
                "--out",
                str(out),
                "--updated",
                "2026-11-01",
            ]
        )
        == 2
    )
    assert train_cli(["report", "--out", str(out), "--updated", "2026-11-01"]) == 2
    assert (
        train_cli(["report", "--runs", str(golden), "--out", str(out), "--updated", "2026-11-01"])
        == 0
    )
    assert "With the guard on" in (out / "eval-report.md").read_text()


def test_the_rules_only_guard_decides_in_under_5_ms(small_build: Path, tmp_path: Path) -> None:
    """Warm-up first, then a large sample; record writing included. Prints what it measured."""
    rows = [
        row for split in ("train", "validation", "test") for row in read_split(small_build, split)
    ]
    assert len(rows) > 100
    policies = {world: load_policy(f"{world}/default") for world in ("payments", "security")}
    guard = GuardedBackend(policies, RulesOnlyBackend(), log=DecisionLog(tmp_path / "d.jsonl"))
    for n in range(WARM_UP):
        guard.decide(rows[n % len(rows)].request)
    timings = []
    for n in range(SAMPLE):
        request = rows[n % len(rows)].request
        start = time.perf_counter()
        guard.decide(request)
        timings.append((time.perf_counter() - start) * 1000)
    timings.sort()
    p50, p99 = timings[len(timings) // 2], timings[int(len(timings) * 0.99) - 1]
    machine = (
        f"{platform.machine()} {platform.processor() or platform.system()}, "
        f"{os.cpu_count()} CPUs, Python {platform.python_version()}"
    )
    print(
        f"\nrules-only guard: {SAMPLE} decisions after {WARM_UP} warm-up: "
        f"p50 {p50:.3f} ms, p99 {p99:.3f} ms on {machine}"
    )
    assert p50 < LIMIT_MS
    assert p99 < LIMIT_MS
