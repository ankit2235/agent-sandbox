"""The worlds' training modules: found through entry points and checked against their WorldSpec."""

from types import SimpleNamespace

import pytest

from guard.actions import TrainingError, actions, from_module
from kit.world import worlds


def test_both_worlds_register_a_training_module() -> None:
    assert {"payments", "security"} <= set(actions.names())
    payments = actions.get("payments")
    assert payments.high_impact == {"pay", "checkout", "create_mandate", "request_refund"}
    security = actions.get("security")
    assert security.high_impact == {
        "isolate_host",
        "reimage_host",
        "disable_account",
        "reset_credentials",
        "block_indicator",
    }
    assert "restore" not in security.high_impact


def test_every_high_impact_tool_is_a_world_tool() -> None:
    for name in ("payments", "security"):
        assert actions.get(name).high_impact <= set(worlds.get(name).tool_names)


def test_the_reason_vocabulary_covers_failure_types_and_rules() -> None:
    vocabulary = actions.get("payments").reason_vocabulary()
    assert "payments.overspend" in vocabulary
    assert "payments.rule.over-per-txn-cap" in vocabulary
    assert all(text for text in vocabulary.values())


def _module(**changes: object) -> SimpleNamespace:
    base = {
        "HIGH_IMPACT": ("pay",),
        "ASK_TOOL": "ask_user",
        "CAUSED_BY_ACTION": ("payments.overspend",),
        "ALWAYS_BLOCK": (),
        "policy": lambda task: {},
        "context": lambda task, state, tool, args: {},
        "RULES": [],
    }
    base.update(changes)
    return SimpleNamespace(**base)


def test_a_module_that_fits_is_accepted() -> None:
    assert from_module("payments", _module()).high_impact == {"pay"}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"HIGH_IMPACT": ("teleport",)}, "unknown tools"),
        ({"ASK_TOOL": "shout"}, "unknown tools"),
        ({"CAUSED_BY_ACTION": ("payments.nonsense",)}, "not a registered failure type"),
        ({"ALWAYS_BLOCK": ("payments.duplicate_payment",)}, "subset"),
        (
            {"RULES": [{"id": "Bad", "decision": "block", "summary": "s", "check": bool}]},
            "dotted id",
        ),
        (
            {
                "RULES": [
                    {"id": "security.rule.x", "decision": "block", "summary": "", "check": bool}
                ]
            },
            "namespace",
        ),
        (
            {
                "RULES": [
                    {"id": "payments.rule.x", "decision": "allow", "summary": "", "check": bool}
                ]
            },
            "needs a decision",
        ),
    ],
)
def test_a_module_that_does_not_fit_is_refused(changes: dict, message: str) -> None:
    with pytest.raises(TrainingError, match=message):
        from_module("payments", _module(**changes))


def test_a_module_missing_something_is_refused() -> None:
    incomplete = _module()
    del incomplete.RULES
    with pytest.raises(TrainingError, match="lacks"):
        from_module("payments", incomplete)


def test_repeated_rule_ids_are_refused() -> None:
    rule = {"id": "payments.rule.x", "decision": "block", "summary": "", "check": bool}
    with pytest.raises(TrainingError, match="repeat"):
        from_module("payments", _module(RULES=[rule, rule]))
