import pytest

from kit.examples.hello_world import world_spec
from kit.world import ReadOnlyService, StateView, WorldRegistry, WorldSpec, load_grader, worlds


class FakeService:
    name = "fake"

    def __init__(self) -> None:
        self.data = {"count": 1}
        self.restored = 0

    def snapshot(self) -> bytes:
        return b"{}"

    def restore(self, blob: bytes) -> None:
        self.restored += 1

    def query(self, resource: str, /, **params: object) -> object:
        return self.data


def test_installed_worlds_are_found_through_entry_points() -> None:
    assert "hello_world" in worlds.names()
    assert worlds.get("hello_world").version == "v0.1"


def test_unknown_world_lists_what_is_installed() -> None:
    with pytest.raises(KeyError, match="installed worlds"):
        worlds.get("nope")


def test_entry_point_name_must_match_the_world_name() -> None:
    registry = WorldRegistry()
    spec = world_spec()
    registry.register(spec)
    assert registry.names() == sorted({*registry.names()})
    assert spec.name in registry


def test_graders_get_a_read_only_view_of_services() -> None:
    service = FakeService()
    view = StateView([service])
    assert isinstance(view["fake"], ReadOnlyService)
    assert view["fake"].query("state") == {"count": 1}
    assert not hasattr(view["fake"], "snapshot")
    assert not hasattr(view["fake"], "restore")


def test_state_view_names_its_services_on_a_miss() -> None:
    with pytest.raises(KeyError, match="this world has"):
        StateView([FakeService()])["rails"]


def test_validate_task_rejects_a_foreign_world(make_task) -> None:
    spec = world_spec()
    with pytest.raises(ValueError, match="is for world"):
        spec.validate_task(make_task(world="payments"))


def test_validate_task_rejects_unknown_categories(make_task) -> None:
    with pytest.raises(ValueError, match="unknown category"):
        world_spec().validate_task(make_task(category="rails"))


def test_validate_task_rejects_tools_the_world_does_not_have(make_task) -> None:
    with pytest.raises(ValueError, match="does not have"):
        world_spec().validate_task(make_task(allowed_tools=["pay"]))


def test_validate_task_checks_initial_state_against_the_world_schema(make_task) -> None:
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        world_spec().validate_task(make_task(initial_state={"start": 0}))


def test_task_source_loads_the_packaged_tasks() -> None:
    source = world_spec().task_source()
    assert source.splits() == ["v0.1"]
    assert {task.id for task in source.tasks} == {
        "hello-count-to-three",
        "hello-respect-ceiling",
    }


def test_world_without_tasks_dir_says_so() -> None:
    spec = WorldSpec(
        name="bare",
        version="v0.1",
        categories=["x"],
        tool_names=[],
        episode_factory=lambda task, clock, rng: None,  # type: ignore[arg-type, return-value]
    )
    with pytest.raises(ValueError, match="does not ship a tasks directory"):
        spec.task_source()


def test_load_grader_reports_bad_paths() -> None:
    with pytest.raises(ImportError, match="cannot import grader module"):
        load_grader("kit.nope:grader")
    with pytest.raises(AttributeError, match="has no grader"):
        load_grader("kit.examples.hello_world.graders:nope")
    with pytest.raises(TypeError, match="not callable"):
        load_grader("kit.examples.hello_world.graders:FAILURE_OVERSHOOT")


def test_registering_a_world_registers_its_failure_types() -> None:
    from kit.failures import failure_types

    worlds.get("hello_world")
    assert "hello_world.overshoot" in failure_types
    assert failure_types.high_impact_ids("hello_world") == {"hello_world.overshoot"}
