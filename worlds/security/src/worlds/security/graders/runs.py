"""Golden and wrong runs: scripted tool calls, kept as data beside the graders.

Every task has a run file, ``graders/runs/<task-id>.yaml``, in the kit's run
file format (:mod:`kit.scripted`, decision 86). This module binds the kit's
player to this world: its runs directory and its WorldSpec.
"""

from __future__ import annotations

from pathlib import Path

from kit import scripted
from kit.scripted import GOLDEN, Run, RunError, RunFile, ScriptRun, Step
from kit.types import TaskSpec

from .. import world_spec

RUNS_DIR = Path(__file__).parent / "runs"


def load_runs(task_id: str, directory: Path = RUNS_DIR) -> RunFile:
    """Read one task's run file."""
    return scripted.load_runs(task_id, directory)


def play(task: TaskSpec, run: Run) -> ScriptRun:
    """Run a scripted run on a fresh episode, grade it, and check it went as scripted."""
    return scripted.play(world_spec(), task, run)


__all__ = ["GOLDEN", "RUNS_DIR", "Run", "RunError", "RunFile", "Step", "load_runs", "play"]
