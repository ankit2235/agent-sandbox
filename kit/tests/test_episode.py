import pytest

from kit.clock import EPOCH, ScheduledEvent, SimClock
from kit.episode import BaseEpisode, ToolContext, ToolResult, ToolRouter, ToolSpec
from kit.errors import EpisodeFinished, ToolRejected
from kit.rng import SeededRNG
from kit.service import JsonStore
from kit.trace import DiffEntry
from kit.types import TaskSpec, ToolCall


class Store:
    name = "store"

    def __init__(self) -> None:
        self.store = JsonStore({"value": 0})

    def snapshot(self) -> bytes:
        return self.store.snapshot()

    def restore(self, blob: bytes) -> None:
        self.store.restore(blob)

    def query(self, resource: str, /, **params: object) -> object:
        return dict(self.store.data)


def build_router() -> ToolRouter:
    router = ToolRouter()

    @router.tool("read", "Read the value.")
    def read(args: dict, context: ToolContext) -> ToolResult:
        service = context.services["store"]
        return ToolResult(text=f"value={service.store.data['value']}")

    @router.tool("write", "Set the value.")
    def write(args: dict, context: ToolContext) -> ToolResult:
        service = context.services["store"]
        if "value" not in args:
            raise ToolRejected("missing_value", "value is required")
        before = service.store.data["value"]
        service.store.data["value"] = args["value"]
        return ToolResult(
            text="written",
            data={"value": args["value"]},
            state_diff=[
                DiffEntry(op="update", path="store/value", before=before, after=args["value"])
            ],
        )

    @router.tool("stop", "Finish the task.", terminal=True)
    def stop(args: dict, context: ToolContext) -> ToolResult:
        return ToolResult(text="stopped")

    return router


class Episode(BaseEpisode):
    world = "test_world"

    def __init__(self, clock: SimClock, rng: SeededRNG) -> None:
        self.service = Store()
        super().__init__([self.service], build_router(), clock, rng)
        self.events_seen: list[str] = []

    def _setup(self, task: TaskSpec) -> None:
        self.service.store.data = {"value": task.initial_state.get("value", 0)}
        for offset, name in task.initial_state.get("events", []):
            self._clock.schedule_in(offset, name)

    def _on_event(self, event: ScheduledEvent) -> list[DiffEntry]:
        self.events_seen.append(event.name)
        return [DiffEntry(op="update", path="store/event", after=event.name)]


def task_for(**initial: object) -> TaskSpec:
    return TaskSpec(
        id="ep-1",
        version="v0.1",
        world="test_world",
        category="basics",
        difficulty="easy",
        instruction="Do the thing.",
        initial_state=dict(initial),
        allowed_tools=["read", "write", "stop"],
        max_steps=4,
        grader="kit.examples.hello_world.graders:reaches_target",
    )


@pytest.fixture
def episode(clock: SimClock, rng: SeededRNG) -> Episode:
    return Episode(clock, rng)


def test_reset_returns_the_instruction_and_snapshots(episode: Episode) -> None:
    observation = episode.reset(task_for(value=5))
    assert observation.text == "Do the thing."
    assert observation.sim_time == EPOCH
    assert episode.snapshot_all()["store"] == episode.service.snapshot()


def test_each_step_advances_simulated_time(episode: Episode) -> None:
    episode.reset(task_for())
    first = episode.step(ToolCall(tool="read"))
    second = episode.step(ToolCall(tool="read"))
    assert second.observation.sim_time > first.observation.sim_time


def test_tools_write_one_trace_step_with_the_state_diff(episode: Episode) -> None:
    episode.reset(task_for(value=1))
    episode.step(ToolCall(tool="write", args={"value": 9}))
    (step,) = episode.trace.tool_calls("write")
    assert step.state_diff[0].before == 1
    assert step.state_diff[0].after == 9
    assert step.error is None


def test_a_refusal_is_data_not_a_crash(episode: Episode) -> None:
    episode.reset(task_for())
    result = episode.step(ToolCall(tool="write", args={}))
    assert result.observation.error is not None
    assert result.observation.error.code == "missing_value"
    assert not result.done


def test_tools_outside_allowed_tools_are_refused_and_recorded(episode: Episode) -> None:
    episode.reset(task_for().model_copy(update={"allowed_tools": ["read"]}))
    result = episode.step(ToolCall(tool="write", args={"value": 1}))
    assert result.observation.error is not None
    assert result.observation.error.code == "tool_not_allowed"
    assert result.info["unauthorised_tools"] == ["write"]
    assert episode.service.store.data["value"] == 0


def test_unknown_tools_are_refused(episode: Episode) -> None:
    episode.reset(task_for())
    result = episode.step(ToolCall(tool="teleport"))
    assert result.observation.error is not None
    assert result.observation.error.code == "unknown_tool"


def test_a_terminal_tool_ends_the_episode(episode: Episode) -> None:
    episode.reset(task_for())
    result = episode.step(ToolCall(tool="stop"))
    assert result.done
    with pytest.raises(EpisodeFinished):
        episode.step(ToolCall(tool="read"))


def test_the_step_cap_ends_the_episode(episode: Episode) -> None:
    episode.reset(task_for())
    for _ in range(3):
        result = episode.step(ToolCall(tool="read"))
        assert not result.done
    result = episode.step(ToolCall(tool="read"))
    assert result.done
    assert result.info["cap_reached"] == "max_steps"


def test_scheduled_events_fire_before_the_next_tool_call(episode: Episode) -> None:
    episode.reset(task_for(events=[[1, "limit_change"]]))
    episode.step(ToolCall(tool="read"))
    assert episode.events_seen == ["limit_change"]
    assert [step.event for step in episode.trace.events()] == ["limit_change"]


def test_close_restores_the_baseline(episode: Episode) -> None:
    episode.reset(task_for(value=2))
    episode.step(ToolCall(tool="write", args={"value": 77}))
    episode.close()
    assert episode.service.store.data["value"] == 2


def test_tasks_may_not_allow_tools_the_world_lacks(episode: Episode) -> None:
    with pytest.raises(ValueError, match="does not have"):
        episode.reset(task_for().model_copy(update={"allowed_tools": ["pay"]}))


def test_duplicate_tool_names_are_a_programming_error() -> None:
    router = ToolRouter(
        [ToolSpec(name="a", description="d", handler=lambda args, ctx: ToolResult(text=""))]
    )
    with pytest.raises(ValueError, match="already registered"):
        router.register(
            ToolSpec(name="a", description="d", handler=lambda args, ctx: ToolResult(text=""))
        )
