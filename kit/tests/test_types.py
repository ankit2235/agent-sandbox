import datetime as dt

import pytest
from pydantic import ValidationError

from kit.failures import FailureTypeRegistry
from kit.types import (
    CheckResult,
    FailureTypeDef,
    GradeResult,
    Observation,
    Signoff,
    TaskSpec,
    ToolCall,
)


def test_task_is_frozen_and_rejects_unknown_fields(task: TaskSpec) -> None:
    with pytest.raises(ValidationError):
        task.id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TaskSpec.model_validate({**task.model_dump(), "surprise": 1})


@pytest.mark.parametrize("bad_id", ["Has Caps", "with space", "", "has/slash"])
def test_task_ids_are_slugs(bad_id: str, make_task) -> None:
    with pytest.raises(ValidationError):
        make_task(id=bad_id)


@pytest.mark.parametrize("bad_grader", ["module.only", "a:b:c", ":missing", "mod:"])
def test_grader_must_be_module_colon_callable(bad_grader: str, make_task) -> None:
    with pytest.raises(ValidationError):
        make_task(grader=bad_grader)


def test_max_steps_must_be_positive(make_task) -> None:
    with pytest.raises(ValidationError):
        make_task(max_steps=0)


def test_run_key_identifies_the_task(task: TaskSpec) -> None:
    assert task.run_key == "hello_world/v0.1/t-1"


def test_signoff_needs_a_date() -> None:
    signoff = Signoff(name="A Reviewer", role="payments ops", date=dt.date(2026, 10, 18))
    assert signoff.date.year == 2026


def test_failure_type_ids_must_be_namespaced() -> None:
    with pytest.raises(ValidationError):
        FailureTypeDef(id="overspend", label="x", description="y")
    with pytest.raises(ValidationError):
        FailureTypeDef(id="a.b.c", label="x", description="y")
    assert (
        FailureTypeDef(id="payments.overspend", label="x", description="y").namespace == "payments"
    )


def test_grade_result_rejects_unregistered_failure_types() -> None:
    with pytest.raises(ValidationError):
        GradeResult(passed=False, score=0.0, checks=[], failure_type="payments.not_registered")


def test_grade_result_accepts_kit_failure_types() -> None:
    result = GradeResult(
        passed=False,
        score=0.5,
        checks=[CheckResult(name="a", passed=True), CheckResult(name="b", passed=False)],
        failure_type="kit.incomplete",
    )
    assert [check.name for check in result.failed_checks] == ["b"]


def test_score_is_bounded() -> None:
    with pytest.raises(ValidationError):
        GradeResult(passed=True, score=1.5, checks=[])


def test_kit_failure_types_are_registered_once() -> None:
    registry = FailureTypeRegistry()
    definition = FailureTypeDef(id="kit.other", label="Other", description="d")
    registry.register(definition)
    registry.register(definition)
    assert len(registry) == 1


def test_observation_requires_sim_time() -> None:
    with pytest.raises(ValidationError):
        Observation(text="hi")  # type: ignore[call-arg]


def test_tool_call_args_default_to_empty() -> None:
    assert ToolCall(tool="get_count").args == {}


def test_check_result_names_only_registered_failure_types() -> None:
    assert CheckResult(name="c", passed=False, failure_type="kit.incomplete").failure_type
    assert CheckResult(name="c", passed=True).failure_type is None
    with pytest.raises(ValidationError, match="not registered"):
        CheckResult(name="c", passed=False, failure_type="nowhere.thing")
