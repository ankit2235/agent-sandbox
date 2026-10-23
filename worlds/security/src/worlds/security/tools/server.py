"""Serve one security task's tools over MCP, on stdin and stdout.

    python -m worlds.security.tools.server path/to/task.yaml [--seed N]

The kit's :class:`kit.mcp_bridge.McpBridge` does the serving, unchanged; this
module only builds the episode for a task file.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import anyio

from kit.clock import EPOCH, SimClock
from kit.mcp_bridge import McpBridge
from kit.rng import SeededRNG
from kit.tasks import load_task

from .. import world_spec
from ..episode import SecurityEpisode


def bridge_for(episode: SecurityEpisode, name: str = "sandbox-security") -> McpBridge:
    """An MCP bridge over an episode that has already been reset with its task."""
    return McpBridge(episode, episode.router, episode.task, name=name)


def main(argv: Sequence[str] | None = None) -> int:
    """Load a task, reset the episode, and serve it until the client disconnects."""
    parser = argparse.ArgumentParser(prog="python -m worlds.security.tools.server")
    parser.add_argument("task", help="a security task YAML file")
    parser.add_argument("--seed", type=int, default=None, help="override the task's seed")
    args = parser.parse_args(argv)
    task = load_task(args.task)
    seed = task.seed if args.seed is None else args.seed
    episode = world_spec().make_episode(task, SimClock(EPOCH), SeededRNG(seed))
    assert isinstance(episode, SecurityEpisode)
    episode.reset(task)
    try:
        anyio.run(bridge_for(episode).serve_stdio)
    finally:
        episode.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
