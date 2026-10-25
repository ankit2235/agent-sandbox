"""The calibration report: planned, run with the fake models, and judged per modifier."""

import json

import pytest

from kit.world import worlds
from runner.calibrate import calibrate, markdown, plan, run_dir, verdict, write_report
from runner.cli import main
from runner.runs import resolve_task_set
from runner.verify import verify
from stress.core import base_id, registry


def test_the_plan_samples_stressed_tasks_that_have_a_trap() -> None:
    chosen = plan(worlds.get("payments"), ["flaky_rails", "pressure"], 3, seed=1)
    assert sorted(chosen.stressed) == ["flaky_service", "pressure"], "aliases resolve"
    assert all(len(tasks) == 3 for tasks in chosen.stressed.values())
    assert plan(worlds.get("payments"), ["flaky_rails", "pressure"], 3, seed=1) == chosen
    assert set(chosen.base_ids) == {base_id(t) for ts in chosen.stressed.values() for t in ts}


def test_an_unknown_modifier_is_named() -> None:
    with pytest.raises(KeyError, match="unknown security modifier"):
        plan(worlds.get("security"), ["fake_offer"], 2, seed=0)


def test_the_verdicts() -> None:
    ok = {
        "a": {"base": 80, "stressed": 40, "drop": 40},
        "b": {"base": 60, "stressed": 58, "drop": 2},
    }
    assert verdict(ok) == "ok"
    easy = {
        "a": {"base": 80, "stressed": 79, "drop": 1},
        "b": {"base": 0, "stressed": 0, "drop": 0},
    }
    assert verdict(easy) == "too_easy"
    hard = {
        "a": {"base": 90, "stressed": 0, "drop": 90},
        "b": {"base": 50, "stressed": 0, "drop": 50},
    }
    assert verdict(hard) == "too_hard"


def test_the_stressed_set_is_one_modifier_per_task_and_stable() -> None:
    for world in ("payments", "security"):
        spec = worlds.get(world)
        tasks = resolve_task_set(spec, f"{world}-v0.1-stressed")
        assert tasks == resolve_task_set(spec, f"{world}-v0.1-stressed")
        assert all(len(t.stress) == 1 and t.visibility == "public" for t in tasks)
        assert {t.stress[0] for t in tasks} == set(registry.get(world).names())


def test_calibrate_with_the_fake_models(tmp_path) -> None:
    out = tmp_path / "calibration"
    report = calibrate(
        "payments",
        ["fake:golden", "fake:wrong"],
        out,
        modifiers=["limit_shift", "fake_offer"],
        per_modifier_count=2,
        concurrency=4,
    )
    rows = {row["name"]: row for row in report["modifiers"]}
    assert sorted(rows) == ["fake_offer", "shifted_constraint"]
    golden = rows["fake_offer"]["results"]["fake:golden"]
    assert golden == {"base": 100.0, "stressed": 100.0, "drop": 0.0}
    wrong = rows["shifted_constraint"]["results"]["fake:wrong"]
    assert wrong["stressed"] == 0.0, "the trap run takes the bait"
    assert rows["fake_offer"]["verdict"] == "too_easy", "the fakes are not affected"
    assert report["stressed_set"]["fake:golden"]["overall"] == 100.0
    assert report["stressed_set"]["fake:wrong"]["overall"] == 0.0
    assert report["best_stressed"] == 100.0 and not report["best_under_target"]
    saved = json.loads((out / "report.json").read_text())
    assert saved == report
    page = (out / "report.md").read_text()
    assert page == markdown(report) and "| fake_offer | 2 |" in page
    assert write_report(out) == report, "the report rebuilds from the run directories"
    assert verify(run_dir(out, "fake:wrong", "stressed-set")).ok, "stressed runs reproduce"
    again = calibrate(
        "payments",
        ["fake:golden", "fake:wrong"],
        out,
        modifiers=["limit_shift", "fake_offer"],
        per_modifier_count=2,
    )
    assert again == report, "a second call resumes: every run is already done"


def test_a_report_needs_finished_runs(tmp_path) -> None:
    out = tmp_path / "empty"
    out.mkdir()
    (out / "plan.json").write_text(
        json.dumps(
            {
                "world": "payments",
                "models": ["fake:golden"],
                "per_modifier": 1,
                "seed": 0,
                "modifiers": {"pressure": []},
            }
        )
    )
    with pytest.raises(ValueError, match="no completed runs"):
        write_report(out)


def test_the_cli_lists_modifiers_and_rebuilds_a_report(tmp_path, capsys) -> None:
    assert main(["stress", "list"]) == 0
    listed = capsys.readouterr().out
    assert "flaky_service" in listed and "(also: flaky_rails)" in listed
    assert "(also: conflicting_instructions)" in listed
    out = tmp_path / "cal"
    args = [
        "stress",
        "calibrate",
        "--world",
        "payments",
        "--model",
        "fake:golden",
        "--modifier",
        "lookalike_payee",
        "--per-modifier",
        "1",
        "--out",
        str(out),
        "--concurrency",
        "4",
    ]
    assert main(args) == 0
    assert main(["stress", "report", str(out)]) == 0
    assert "| lookalike_payee | 1 |" in capsys.readouterr().out
