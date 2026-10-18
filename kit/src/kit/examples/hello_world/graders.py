"""The hello-world grader: did the agent land exactly on the target, and stop?"""

from __future__ import annotations

from kit.trace import GraderTrace
from kit.types import CheckResult, GradeResult, TaskSpec
from kit.world import StateView

FAILURE_OVERSHOOT = "hello_world.overshoot"
FAILURE_WRONG_COUNT = "hello_world.wrong_count"


def reaches_target(state: StateView, trace: GraderTrace, task: TaskSpec) -> GradeResult:
    """Pass only if the counter ends on the target and the agent said it was finished."""
    target = int(task.initial_state["target"])
    final = int(state["counter"].query("count")["count"])
    history = state["counter"].query("history")
    peak = max([entry["count"] for entry in history], default=final)

    checks = [
        CheckResult(
            name="counter_on_target",
            passed=final == target,
            detail=f"counter is {final}, target is {target}",
        ),
        CheckResult(
            name="never_overshot",
            passed=peak <= target,
            detail=f"highest value reached was {peak}",
        ),
        CheckResult(
            name="declared_finished",
            passed=trace.count("finish") == 1,
            detail=f"finish called {trace.count('finish')} time(s)",
        ),
    ]
    passed = all(check.passed for check in checks)
    failure: str | None = None
    if not passed:
        if peak > target:
            failure = FAILURE_OVERSHOOT
        elif final != target:
            failure = FAILURE_WRONG_COUNT
        else:
            failure = "kit.incomplete"
    return GradeResult(
        passed=passed,
        score=sum(check.passed for check in checks) / len(checks),
        checks=checks,
        failure_type=failure,
    )
