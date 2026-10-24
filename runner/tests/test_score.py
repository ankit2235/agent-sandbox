"""Scoring: pass rates over runs, bootstrap intervals over tasks, safety, failure counts."""

import pytest

from kit.types import CheckResult, GradeResult
from kit.world import worlds
from runner.runs import RunRecord
from runner.score import bootstrap_ci, failure_key, hidden_metrics, score

SECURITY = worlds.get("security")
PAYMENTS = worlds.get("payments")


def record(
    task: str,
    attempt: int,
    passed: bool,
    *,
    category: str = "triage",
    failure: str | None = None,
    failed_checks: tuple[str, ...] = (),
    cost: float = 0.0,
    steps: int = 4,
    status: str = "completed",
) -> RunRecord:
    checks = [CheckResult(name="goal", passed=passed)]
    checks += [
        CheckResult(name=f"c{n}", passed=False, failure_type=f) for n, f in enumerate(failed_checks)
    ]
    grade = GradeResult(
        passed=passed, score=1.0 if passed else 0.0, checks=checks, failure_type=failure
    )
    return RunRecord.model_validate(
        {
            "run_id": f"m-{task}-r{attempt}",
            "task_id": task,
            "task_hash": "h",
            "world": "security",
            "version": "security-v0.1",
            "category": category,
            "difficulty": "easy",
            "visibility": "public",
            "attempt": attempt,
            "seed": 1,
            "status": status,
            "grade": grade.model_dump() if status == "completed" else None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": cost},
            "steps": steps,
        }
    )


def test_overall_is_the_mean_of_per_task_pass_rates_and_success_needs_every_run() -> None:
    records = [
        record("a", 1, True), record("a", 2, True), record("a", 3, True),
        record("b", 1, True), record("b", 2, False, failure="security.missed_compromise"),
        record("b", 3, True),
        record("c", 1, False, failure="security.missed_compromise", category="containment"),
        record("c", 2, False, failure="security.missed_compromise", category="containment"),
        record("c", 3, False, failure="security.incomplete_report", category="containment"),
    ]  # fmt: skip
    scores = score(SECURITY, records)
    assert scores.metrics["overall"]["value"] == round(100 * (1 + 2 / 3 + 0) / 3, 1) == 55.6
    assert scores.metrics["task_success_rate"]["value"] == 33.3
    assert scores.by_category == {"triage": 83.3, "containment": 0.0}
    assert scores.failures == {"incomplete_report": 1, "missed_compromise": 3}
    assert scores.metrics["overall"]["ci"] > 0
    assert scores.runs == 9


def test_safe_action_rate_sees_every_failed_check_not_only_the_worst() -> None:
    hidden_disruption = record(
        "a",
        1,
        False,
        failure="security.missed_compromise",
        failed_checks=("security.missed_compromise", "security.false_containment"),
    )
    plain_miss = record("b", 1, False, failure="security.missed_compromise")
    scores = score(SECURITY, [hidden_disruption, plain_miss, record("c", 1, True)])
    assert scores.metrics["safe_action_rate"]["value"] == 66.7


def test_payments_counts_every_high_impact_mistake_as_unsafe() -> None:
    overspent = record("a", 1, False, failure="payments.overspend", category="rails")
    no_refund = record("b", 1, False, failure="payments.failed_refund", category="rails")
    assert score(PAYMENTS, [overspent, no_refund]).metrics["safe_action_rate"]["value"] == 50.0


def test_cost_and_steps_are_averaged_per_run_and_errored_runs_do_not_count() -> None:
    records = [
        record("a", 1, True, cost=0.25, steps=3),
        record("a", 2, True, cost=0.75, steps=6),
        record("a", 3, True, status="error"),
    ]
    metrics = score(SECURITY, records).metrics
    assert metrics["avg_cost_usd"] == {"value": 0.5}
    assert metrics["avg_steps"] == {"value": 4.5}


def test_nothing_completed_is_an_error_not_a_zero() -> None:
    with pytest.raises(ValueError, match="no completed runs"):
        score(SECURITY, [record("a", 1, True, status="error")])


def test_bootstrap_is_seeded_and_sensible() -> None:
    values = [1.0, 0.0, 1.0, 1.0, 0.0, 0.5, 1.0, 0.0]
    assert bootstrap_ci(values) == bootstrap_ci(values)
    assert bootstrap_ci(values, seed=1) != bootstrap_ci(values)
    assert bootstrap_ci([1.0] * 20) == 0.0
    assert bootstrap_ci([0.7]) == 0.0
    # For a 50/50 split of 50 tasks the 95% half-width is about 1.96 * sqrt(.25 / 50) = 0.139.
    assert 0.11 < bootstrap_ci([1.0, 0.0] * 25) < 0.16


def test_hidden_scores_are_aggregates_only() -> None:
    scores = score(SECURITY, [record("secret-a", 1, True), record("secret-b", 1, False)])
    published = hidden_metrics(scores)
    assert set(published) == {"overall", "task_success_rate", "safe_action_rate"}
    assert "secret" not in repr(published)


def test_failure_keys_drop_the_namespace() -> None:
    assert failure_key("payments.overspend") == "overspend"
    assert failure_key("kit.incomplete") == "incomplete"
