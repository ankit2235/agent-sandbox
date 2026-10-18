import pytest

from kit.failures import KIT_FAILURE_TYPES, FailureTypeRegistry
from kit.types import FailureTypeDef


def registry() -> FailureTypeRegistry:
    return FailureTypeRegistry(KIT_FAILURE_TYPES)


def test_kit_owns_three_generic_types() -> None:
    assert registry().ids("kit") == ["kit.incomplete", "kit.other", "kit.unauthorised_action"]


def test_world_types_must_use_the_world_namespace() -> None:
    reg = registry()
    with pytest.raises(ValueError, match="namespaced"):
        reg.register_world(
            "payments", [FailureTypeDef(id="security.late", label="l", description="d")]
        )


def test_two_worlds_can_use_the_same_short_name() -> None:
    reg = registry()
    reg.register_world(
        "security", [FailureTypeDef(id="security.late_escalation", label="l", description="d")]
    )
    reg.register_world(
        "drug_safety",
        [FailureTypeDef(id="drug_safety.late_submission", label="l", description="d")],
    )
    assert "security.late_escalation" in reg
    assert "drug_safety.late_submission" in reg


def test_redefining_an_id_with_different_content_is_an_error() -> None:
    reg = registry()
    reg.register(FailureTypeDef(id="payments.overspend", label="A", description="d"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(FailureTypeDef(id="payments.overspend", label="B", description="d"))


def test_high_impact_ids_feed_the_clean_run_metric() -> None:
    reg = registry()
    reg.register_world(
        "payments",
        [
            FailureTypeDef(id="payments.overspend", label="l", description="d", high_impact=True),
            FailureTypeDef(id="payments.slow", label="l", description="d"),
        ],
    )
    assert reg.high_impact_ids("payments") == {"payments.overspend"}


def test_unknown_id_names_what_is_registered() -> None:
    with pytest.raises(KeyError, match="registered"):
        registry().get("payments.nope")
