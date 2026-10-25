"""K4 and K5: counterpart roles and extra task sets on a WorldSpec."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kit.examples.hello_world import world_spec
from kit.roles import Role, TurnTaking, agent_tools, is_multi_agent, role_tools
from kit.types import ToolCall


def with_roles(**extra: object):
    spec = world_spec()
    return spec.model_copy(update=extra)


def build(**extra: object):
    """A WorldSpec built (and so validated) with the hello-world fields plus ``extra``."""
    spec = world_spec()
    fields = {name: getattr(spec, name) for name in type(spec).model_fields}
    return type(spec)(**{**fields, **extra})


def test_a_role_owns_some_of_the_worlds_tools() -> None:
    spec = build(roles=[Role(name="judge", tools=["finish"])])
    assert spec.counterpart_tools() == {"finish"}


def test_role_names_are_slugs_and_roles_own_at_least_one_tool() -> None:
    with pytest.raises(ValidationError, match="must match"):
        Role(name="Seller Bot", tools=["finish"])
    with pytest.raises(ValidationError):
        Role(name="seller", tools=[])


def test_a_role_may_not_name_a_tool_the_world_lacks() -> None:
    with pytest.raises(ValueError, match="does not have"):
        build(roles=[Role(name="judge", tools=["teleport"])])


def test_two_roles_may_not_share_a_tool_or_a_name() -> None:
    with pytest.raises(ValueError, match="belongs to both"):
        build(roles=[Role(name="a", tools=["finish"]), Role(name="b", tools=["finish"])])
    with pytest.raises(ValueError, match="repeat"):
        build(roles=[Role(name="a", tools=["finish"]), Role(name="a", tools=["increment"])])


def test_a_task_is_multi_agent_when_it_allows_a_roles_tool(make_task) -> None:
    roles = [Role(name="judge", tools=["finish"])]
    task = make_task()
    assert is_multi_agent(roles, task)
    assert agent_tools(roles, task) == ["get_count", "increment"]
    assert not is_multi_agent(roles, make_task(allowed_tools=["get_count", "increment"]))
    assert not is_multi_agent([], task)
    assert role_tools(roles) == {"finish"}


def test_a_world_with_roles_refuses_a_task_with_no_tool_list(make_task) -> None:
    spec = build(roles=[Role(name="judge", tools=["finish"])])
    with pytest.raises(ValueError, match="must list allowed_tools"):
        spec.validate_task(make_task(allowed_tools=[]))
    assert spec.validate_task(make_task()).id == "t-1"
    assert world_spec().validate_task(make_task(allowed_tools=[])).id == "t-1"


def test_worlds_without_roles_are_unchanged(make_task) -> None:
    spec = world_spec()
    assert spec.roles == [] and spec.task_sets == {}
    assert not spec.is_multi_agent(make_task())
    assert spec.agent_tools(make_task()) == ["get_count", "increment", "finish"]


@pytest.mark.parametrize("name", ["stressed", "hidden", "Not A Slug"])
def test_extra_task_sets_cannot_take_a_reserved_or_bad_name(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved or not a slug"):
        build(task_sets={name: tmp_path})


def test_extra_task_sets_are_kept_by_name(tmp_path: Path) -> None:
    assert build(task_sets={"agents": tmp_path}).task_sets == {"agents": tmp_path}


def test_turn_taking_is_a_structural_protocol() -> None:
    class Episode:
        def awaiting(self) -> list[str]:
            return ["judge"]

        def brief(self, role: str) -> str:
            return "judge fairly"

        def scripted_turn(self, role: str) -> list[ToolCall]:
            return [ToolCall(tool="finish")]

    assert isinstance(Episode(), TurnTaking)
    assert not isinstance(object(), TurnTaking)
    assert with_roles().roles == []
