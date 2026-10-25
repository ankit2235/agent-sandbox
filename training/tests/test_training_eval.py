"""Evaluation: metrics with family intervals, calibration, the report, and the eval CLI."""

import datetime as dt
import json
from pathlib import Path

import jsonschema
import pytest

from guard.backends.calibration import (
    LOW_CONFIDENCE,
    CalibratedBackend,
    Calibration,
    apply,
    scaled,
)
from guard.decision import BackendResult, Decision
from training.cli import main as train_cli
from training.eval.calibration import fit
from training.eval.metrics import (
    breakdown,
    cost_per_1000,
    ece,
    family_rate,
    percentile,
    robustness,
    summarize,
)
from training.eval.prices import Price, load_prices
from training.eval.report import SCHEMA, leaderboard, losses, markdown
from training.eval.run import Prediction


def _p(
    family: str,
    label: str,
    decision: str,
    *,
    confidence: float = 0.9,
    probabilities: dict | None = None,
    stressed: bool = False,
    tokens: int | None = None,
    latency: float = 10.0,
    split: str = "test",
    error: str | None = None,
) -> Prediction:
    return Prediction(
        example_id=f"{family}-{label}-{decision}-{confidence}",
        world="payments",
        category="rails",
        family=family,
        split=split,
        stressed=stressed,
        label=label,
        result=BackendResult(
            decision=Decision(decision=decision, confidence=confidence),
            backend="b",
            probabilities=probabilities,
            input_tokens=tokens,
            output_tokens=0 if tokens is not None else None,
            error=error,
        ),
        latency_ms=latency,
    )


def test_rates_are_means_over_families() -> None:
    preds = [_p("a", "block", "block"), _p("a", "block", "allow"), _p("b", "block", "block")]
    rate = family_rate(preds, lambda p: p.result.decision.decision == p.label)
    assert rate["value"] == pytest.approx(0.75)
    assert rate["families"] == 2
    assert rate["rows"] == 3


def test_conditional_rates_ignore_rows_that_do_not_count_and_cope_with_none() -> None:
    preds = [_p("a", "allow", "block"), _p("a", "allow", "allow"), _p("b", "block", "allow")]
    metrics = summarize(preds)
    assert metrics["wrongful_block_rate"]["value"] == pytest.approx(0.5)
    assert metrics["missed_mistake_rate"]["value"] == pytest.approx(1.0)
    assert metrics["correct_block_rate"]["families"] == 1
    empty = summarize([_p("a", "allow", "allow")])
    assert empty["missed_mistake_rate"] == {"value": None, "ci": None, "families": 0, "rows": 0}


def test_ece_percentiles_and_costs() -> None:
    sure_and_right = [_p("a", "block", "block", confidence=1.0)] * 4
    assert ece(sure_and_right) == 0.0
    assert ece([_p("a", "block", "allow", confidence=1.0)]) == 1.0
    assert ece([]) is None
    assert percentile([5, 1, 3, 2, 4], 50) == 3
    assert percentile([], 99) is None
    priced = Price(input_per_million_usd=0.1, source="s", checked=dt.date(2026, 10, 25))
    assert cost_per_1000([_p("a", "allow", "allow", tokens=1000)], priced) == pytest.approx(0.1)
    assert cost_per_1000([_p("a", "allow", "allow")], priced) is None
    hourly = Price(per_hour_usd=3.6, source="s", checked=dt.date(2026, 10, 25))
    assert cost_per_1000([_p("a", "allow", "allow", latency=1000.0)], hourly) == pytest.approx(1.0)
    assert cost_per_1000([_p("a", "allow", "allow")], None) is None


def test_every_price_records_when_it_was_checked() -> None:
    prices = load_prices()
    assert prices["openai-decisions"] is not None
    assert prices["openai-decisions"].checked == dt.date(2026, 10, 25)
    assert prices["jev"] is None
    with pytest.raises(ValueError, match="not both"):
        Price(per_hour_usd=1, input_per_million_usd=1, source="s", checked=dt.date(2026, 1, 1))


def test_robustness_compares_families_with_and_without_stress() -> None:
    preds = [
        _p("a", "block", "block"),
        _p("a", "block", "allow", stressed=True),
        _p("b", "allow", "allow"),
        _p("b", "allow", "allow", stressed=True),
        _p("c", "allow", "allow"),
    ]
    found = robustness(preds)
    assert found["families"] == 2
    assert found["drop"] == pytest.approx(0.5)
    assert robustness([_p("c", "allow", "allow")])["drop"] is None


def test_breakdown_is_by_world_and_category() -> None:
    found = breakdown([_p("a", "allow", "allow")])
    assert set(found) == {"overall", "by_world", "by_category"}
    assert "payments/rails" in found["by_category"]


# ----------------------------------------------------------- calibration
def test_an_overconfident_backend_gets_a_temperature_above_one() -> None:
    preds = []
    for i in range(40):
        right = i % 2 == 0
        preds.append(
            _p(
                f"f{i}",
                "block",
                "block" if right else "allow",
                confidence=0.96,
                probabilities={"allow": 0.02, "block": 0.96, "ask_user": 0.02}
                if right
                else {"allow": 0.96, "block": 0.02, "ask_user": 0.02},
                split="validation",
            )
        )
    calibration = fit("b", preds, target_missed=0.02, fitted_on="v")
    assert calibration.temperature > 1.0
    assert calibration.ece_after <= calibration.ece_before


def test_below_the_threshold_nothing_is_a_silent_allow() -> None:
    calibration = Calibration(
        backend="b", temperature=1.0, threshold=0.8, target_missed=0.0, fitted_on="v", rows=1
    )
    low = _p("a", "allow", "allow", probabilities={"allow": 0.6, "block": 0.3, "ask_user": 0.1})
    result = apply(low.result, calibration)
    assert result.decision.decision == "ask_user"
    assert LOW_CONFIDENCE in result.decision.reason_codes
    high = _p("a", "allow", "allow", probabilities={"allow": 0.9, "block": 0.05, "ask_user": 0.05})
    assert apply(high.result, calibration).decision.decision == "allow"
    failed = _p("a", "allow", "ask_user", confidence=0.0, error="timeout")
    assert apply(failed.result, calibration) == failed.result


def test_the_threshold_meets_the_missed_mistake_target_on_validation() -> None:
    preds = [
        _p("a", "block", "allow", confidence=0.55, split="validation"),
        _p("b", "block", "block", confidence=0.95, split="validation"),
        _p("c", "allow", "allow", confidence=0.9, split="validation"),
    ]
    calibration = fit("b", preds, target_missed=0.0, fitted_on="v")
    assert 0.55 < calibration.threshold <= 0.9


def test_scaling_keeps_probabilities_normalised() -> None:
    out = scaled({"allow": 0.7, "block": 0.2, "ask_user": 0.1}, 2.0)
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["allow"] < 0.7
    assert scaled({}, 1.0) == {"allow": 1 / 3, "block": 1 / 3, "ask_user": 1 / 3}


def test_a_calibrated_backend_wraps_any_backend() -> None:
    class Unsure:
        name = "unsure"

        def decide(self, request):
            return _p("a", "allow", "allow", confidence=0.4).result

    calibration = Calibration(
        backend="unsure", temperature=1, threshold=0.5, target_missed=0, fitted_on="v", rows=1
    )
    result = CalibratedBackend(Unsure(), calibration).decide(None)
    assert result.decision.decision == "ask_user"
    assert result.backend == "unsure+calibrated"


# ----------------------------------------------------------------- report
def _result(name: str, kind: str, accuracy: float, set_name: str = "test") -> dict:
    overall = summarize([_p("a", "allow", "allow")])
    overall["accuracy"] = {"value": accuracy, "ci": 0.05, "families": 7, "rows": 100}
    return {
        "format": "guard-eval-v1",
        "backend": {"name": name, "kind": kind, "model": "m"},
        "dataset": "actions-v0.1",
        "set": set_name,
        "rows": 100,
        "metrics": {"overall": overall, "by_world": {}, "by_category": {}},
        "robustness": None,
        "model_versions": ["v1"],
        "price": None,
    }


def test_the_report_says_where_our_model_loses_first() -> None:
    results = [_result("ours", "guard_model", 0.6), _result("jev", "system_one", 0.8)]
    found = losses(results)
    assert found == ["test: jev beats ours on accuracy (0.800 vs 0.600)"]
    text = markdown(results)
    assert text.startswith("## Where our model loses")
    assert "jev beats ours" in text
    assert "Our model is not in" in markdown([_result("jev", "system_one", 0.8)])


def test_the_leaderboard_file_validates_and_discloses() -> None:
    data = leaderboard(
        [_result("ours", "guard_model", 0.6), _result("rules_only", "rules_only", 0.4)],
        dt.date(2026, 11, 1),
    )
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    assert data["updated"] == "2026-11-01"
    assert "built the guard model" in data["disclosure"]
    assert [b["ours"] for b in data["backends"]] == [True, False]


# -------------------------------------------------------------------- CLI
def test_eval_calibrate_and_report_end_to_end(small_build: Path, tmp_path: Path) -> None:
    out = tmp_path / "eval"
    assert (
        train_cli(
            [
                "eval",
                "--backend",
                "rules_only",
                "--data",
                str(small_build),
                "--split",
                "validation",
                "--predictions",
                str(out / "val.jsonl"),
                "--out",
                str(out / "val.json"),
            ]
        )
        == 0
    )
    assert (
        train_cli(
            ["calibrate", "--predictions", str(out / "val.jsonl"), "--out", str(out / "cal.json")]
        )
        == 0
    )
    assert (
        train_cli(
            [
                "eval",
                "--backend",
                "rules_only",
                "--data",
                str(small_build),
                "--calibration",
                str(out / "cal.json"),
                "--limit",
                "5",
                "--out",
                str(out / "test.json"),
            ]
        )
        == 0
    )
    result = json.loads((out / "test.json").read_text())
    assert result["rows"] == 5
    assert result["calibration"]["backend"] == "rules_only"
    assert result["model_versions"]
    assert (
        train_cli(
            [
                "report",
                str(out / "test.json"),
                "--out",
                str(out / "report"),
                "--updated",
                "2026-11-01",
            ]
        )
        == 0
    )
    assert (out / "report" / "guards" / "results.json").exists()


def test_calibrating_on_test_predictions_is_refused(small_build: Path, tmp_path: Path) -> None:
    train_cli(
        [
            "eval",
            "--backend",
            "rules_only",
            "--data",
            str(small_build),
            "--predictions",
            str(tmp_path / "t.jsonl"),
            "--out",
            str(tmp_path / "t.json"),
        ]
    )
    assert (
        train_cli(
            ["calibrate", "--predictions", str(tmp_path / "t.jsonl"), "--out", str(tmp_path / "c")]
        )
        == 2
    )


def test_hidden_eval_writes_no_predictions(small_build: Path, tmp_path: Path) -> None:
    code = train_cli(
        [
            "eval",
            "--backend",
            "rules_only",
            "--data",
            str(small_build),
            "--hidden",
            "--predictions",
            str(tmp_path / "h.jsonl"),
            "--out",
            str(tmp_path / "h.json"),
        ]
    )
    assert code == 2
    assert not (tmp_path / "h.jsonl").exists()


def test_hidden_eval_without_the_private_repository_stops_cleanly(
    small_build: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    code = train_cli(
        [
            "eval",
            "--backend",
            "rules_only",
            "--data",
            str(small_build),
            "--hidden",
            "--out",
            str(tmp_path / "h.json"),
        ]
    )
    assert code == 2
    assert "sandbox hidden check" in capsys.readouterr().out
    assert not (tmp_path / "h.json").exists()


def test_a_judge_without_a_model_id_stops_before_any_call(
    small_build: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = train_cli(
        ["eval", "--backend", "judge-b", "--data", str(small_build), "--out", str(tmp_path / "j")]
    )
    assert code == 2
    assert "no default" in capsys.readouterr().out


def test_the_dataset_card_is_written_from_the_manifest(small_build: Path, tmp_path: Path) -> None:
    assert train_cli(["dataset", "card", str(small_build), "--into", str(tmp_path)]) == 0
    card = (tmp_path / "DATASET_CARD.md").read_text()
    assert "Not decontaminated" in card
    assert "Drug safety is pending" in card
    assert json.loads((tmp_path / "manifest.json").read_text())["name"] == "actions-test"


def test_a_hidden_bucket_of_fewer_than_three_families_shows_counts_only() -> None:
    from training.cli import _bucket

    few = [_p("a", "allow", "allow"), _p("b", "block", "block")]
    assert _bucket(few, None) == {"suppressed": True, "rows": 2, "families": 2}
    enough = [*few, _p("c", "allow", "block")]
    assert "overall" in _bucket(enough, None)
