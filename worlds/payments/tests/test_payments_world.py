"""The payments world as the kit sees it: registry, task schema, cross-service checks."""

import copy

import jsonschema
import pytest
from payments_testkit import USER, base_state, handle, make_task

from kit import failure_types, worlds
from worlds.payments import TOOLS_VERSION, world_spec
from worlds.payments.state import PaymentsState


def test_the_world_is_found_through_its_entry_point() -> None:
    assert "payments" in worlds.names()
    spec = worlds.get("payments")
    assert spec.version == "v0.1"
    assert TOOLS_VERSION == "tools-payments-v0.1"
    assert set(failure_types.ids("payments")) == {
        "payments.overspend", "payments.wrong_merchant", "payments.duplicate_payment",
        "payments.ignored_limit", "payments.fake_discount", "payments.failed_refund",
    }  # fmt: skip


def test_the_task_schema_accepts_a_good_state_and_the_kit_validates_with_it() -> None:
    spec = world_spec()
    jsonschema.validate(base_state(), spec.task_schema)
    spec.validate_task(make_task())


def test_the_task_schema_rejects_unknown_fields() -> None:
    state = base_state()
    state["surprise"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(state, world_spec().task_schema)


def broken(mutate) -> dict:
    state = copy.deepcopy(base_state())
    mutate(state)
    return state


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s.update(user="ghost@sandbox"), "must be a user account"),
        (lambda s: s.update(user=handle("honest-goods")), "must be a user account"),
        (lambda s: s["commerce"]["merchants"][0].update(handle=USER), "must be a merchant"),
        (lambda s: s["commerce"]["products"][0].update(merchant_id="nobody"), "unknown merchants"),
        (lambda s: s["commerce"]["merchants"][0].update(
            behaviour={"ship_after_seconds": 10, "deliver_after_seconds": 5}), "before shipping"),
        (lambda s: s.update(disputes={"inbox": [{"sender": "x", "subject": "y", "body": "z",
                                                 "merchant_id": "nobody"}]}), "unknown merchants"),
        (lambda s: s["commerce"]["offers"][0].update(flat_off_paise=5), "exactly one"),
    ],
)  # fmt: skip
def test_cross_service_references_are_checked(mutate, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PaymentsState.model_validate(broken(mutate))
