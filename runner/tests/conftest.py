"""Fixtures for the runner tests; helpers live in :mod:`runner_testkit`."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from runner_testkit import HELLO_TASKS

from runner.runs import RunConfig


@pytest.fixture
def hello_dir(tmp_path: Path) -> Path:
    """A task directory holding only hello-count-to-three (target 3, finish is terminal)."""
    directory = tmp_path / "tasks"
    directory.mkdir()
    shutil.copy(HELLO_TASKS / "hello-count-to-three.yaml", directory)
    return directory


@pytest.fixture
def hello_config(hello_dir: Path) -> Callable[..., RunConfig]:
    def build(**overrides: object) -> RunConfig:
        payload: dict[str, object] = {
            "world": "hello_world",
            "tasks": str(hello_dir),
            "model": "fake:script",
            "model_name": "Scripted",
            "provider": "fake",
        }
        payload.update(overrides)
        return RunConfig.model_validate(payload)

    return build
