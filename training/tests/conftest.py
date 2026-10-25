"""Fixtures for the training tests: one small real build, shared, and a fresh copy when needed."""

import shutil
from pathlib import Path

import pytest
from training_testkit import SMALL

from training.data.build import build


@pytest.fixture(scope="session")
def small_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A built dataset folder, made once per test session. Do not modify it."""
    out = tmp_path_factory.mktemp("built")
    build(SMALL, out, workers=1)
    return out / SMALL.name


@pytest.fixture
def dataset(small_build: Path, tmp_path: Path) -> Path:
    """A private copy of the small build, safe to tamper with."""
    copy = tmp_path / small_build.name
    shutil.copytree(small_build, copy)
    return copy
