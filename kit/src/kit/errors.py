"""Exceptions the kit raises. Worlds raise :class:`ToolRejected` and nothing else."""

from __future__ import annotations


class SandboxError(Exception):
    """Base class for every error the kit raises."""


class ToolRejected(SandboxError):
    """A tool refused the call for a documented, deterministic reason.

    The episode turns this into a :class:`kit.types.ToolError` the agent can
    read, so a refusal is part of the task, not a crash.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class EpisodeFinished(SandboxError):
    """Raised when a caller steps an episode that has already ended."""


class WorldNotFound(SandboxError):
    """Raised when a task names a world that is not installed."""
