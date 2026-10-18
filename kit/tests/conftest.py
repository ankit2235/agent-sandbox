import datetime as dt

import pytest

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.types import TaskSpec


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def rng() -> SeededRNG:
    return SeededRNG(7)


def build_task(**overrides: object) -> TaskSpec:
    payload: dict[str, object] = {
        "id": "t-1",
        "version": "v0.1",
        "world": "hello_world",
        "category": "basics",
        "difficulty": "easy",
        "instruction": "Raise the counter to 2 and finish.",
        "initial_state": {"start": 0, "target": 2},
        "allowed_tools": ["get_count", "increment", "finish"],
        "max_steps": 5,
        "grader": "kit.examples.hello_world.graders:reaches_target",
        "seed": 3,
    }
    payload.update(overrides)
    return TaskSpec.model_validate(payload)


@pytest.fixture
def make_task():
    """Factory fixture so tests can build task variants without importing conftest."""
    return build_task


@pytest.fixture
def task() -> TaskSpec:
    return build_task()


@pytest.fixture
def start_time() -> dt.datetime:
    return EPOCH
