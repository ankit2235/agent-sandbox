"""The decision contract: the schema, parsing a model's answer, and failing closed."""

import datetime as dt

import jsonschema
import pytest

from guard import (
    DECISION_SCHEMA,
    ActionCall,
    BackendResult,
    Decision,
    DecisionBackend,
    DecisionError,
    DecisionRequest,
    fail_closed,
    parse_decision,
)

NOW = dt.datetime(2026, 1, 1, 9, tzinfo=dt.UTC)


def test_a_valid_decision_parses() -> None:
    text = '{"decision": "block", "reason_codes": ["payments.overspend"], "confidence": 0.9}'
    assert parse_decision(text) == Decision(
        decision="block", reason_codes=["payments.overspend"], confidence=0.9
    )


def test_one_code_fence_is_accepted() -> None:
    text = '```json\n{"decision": "allow", "reason_codes": [], "confidence": 1}\n```'
    assert parse_decision(text).decision == "allow"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("allow", "invalid_json"),
        ('{"decision": "allow"', "invalid_json"),
        ('Sure! {"decision": "allow", "reason_codes": [], "confidence": 1}', "invalid_json"),
        ('{"decision": "maybe", "reason_codes": [], "confidence": 0.5}', "schema"),
        ('{"decision": "allow", "reason_codes": [], "confidence": 1.5}', "schema"),
        ('{"decision": "allow", "reason_codes": ["Overspend"], "confidence": 1}', "schema"),
        ('{"decision": "allow", "reason_codes": ["a.b", "a.b"], "confidence": 1}', "schema"),
        ('{"decision": "allow", "reason_codes": [], "confidence": 1, "why": "x"}', "schema"),
        ('{"decision": "allow", "confidence": 1}', "schema"),
        ("[]", "schema"),
    ],
)
def test_anything_else_is_refused_with_a_code(text: str, code: str) -> None:
    with pytest.raises(DecisionError) as caught:
        parse_decision(text)
    assert caught.value.code == code


def test_the_schema_and_the_model_agree() -> None:
    decision = Decision(decision="ask_user", reason_codes=["guard.timeout"], confidence=0.0)
    jsonschema.validate(decision.model_dump(mode="json"), DECISION_SCHEMA)
    with pytest.raises(ValueError, match="dotted"):
        Decision(decision="allow", reason_codes=["nodot"], confidence=1.0)


def test_failing_closed_never_allows() -> None:
    result = fail_closed("jev", "Timeout!", model="jev-latest")
    assert result.decision.decision == "ask_user"
    assert result.decision.confidence == 0.0
    assert result.decision.reason_codes == ["guard.timeout_"]
    assert result.error == "timeout_"
    assert result.model == "jev-latest"


def test_a_backend_is_anything_with_a_name_and_decide() -> None:
    class Always:
        name = "always"

        def decide(self, request: DecisionRequest) -> BackendResult:
            return BackendResult(
                decision=Decision(decision="allow", confidence=1.0), backend=self.name
            )

    request = DecisionRequest(
        world="w", instruction="do it", action=ActionCall(tool="pay"), sim_time=NOW
    )
    backend = Always()
    assert isinstance(backend, DecisionBackend)
    assert backend.decide(request).decision.decision == "allow"


def test_requests_are_frozen_and_strict() -> None:
    with pytest.raises(ValueError, match="extra"):
        DecisionRequest.model_validate(
            {
                "world": "w",
                "instruction": "i",
                "action": {"tool": "pay"},
                "sim_time": NOW.isoformat(),
                "reasoning": "the agent's thoughts",
            }
        )
