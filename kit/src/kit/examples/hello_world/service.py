"""The counter service: the smallest thing that can implement WorldState."""

from __future__ import annotations

from typing import Any

from kit.clock import SimClock
from kit.errors import ToolRejected
from kit.service import JsonStore
from kit.trace import DiffEntry


class CounterService:
    """A counter with a history, snapshot-able and readable by graders.

    Deliberately has no HTTP layer: the example must import with the kit's own
    dependencies only, so that discovering worlds never needs a web framework.
    Real worlds use :class:`kit.service.AsgiService` instead.
    """

    name = "counter"

    def __init__(self, clock: SimClock) -> None:
        self._clock = clock
        self._store = JsonStore({"count": 0, "ceiling": None, "history": []})

    # ------------------------------------------------------------ WorldState
    def snapshot(self) -> bytes:
        """Canonical JSON of the counter's state."""
        return self._store.snapshot()

    def restore(self, blob: bytes) -> None:
        """Restore a snapshot."""
        self._store.restore(blob)

    def query(self, resource: str, /, **params: Any) -> Any:
        """Read-only admin surface for graders."""
        data = self._store.data
        if resource == "state":
            return dict(data)
        if resource == "count":
            return {"count": data["count"]}
        if resource == "history":
            return list(data["history"])
        msg = f"unknown resource {resource!r}; counter has state, count, history"
        raise KeyError(msg)

    # ----------------------------------------------------------- the service
    def load(self, *, count: int = 0, ceiling: int | None = None) -> None:
        """Set the starting state for a task."""
        self._store.data = {"count": count, "ceiling": ceiling, "history": []}

    @property
    def count(self) -> int:
        """The current count."""
        return int(self._store.data["count"])

    @property
    def ceiling(self) -> int | None:
        """A hard limit the service refuses to cross, if the task set one."""
        value = self._store.data["ceiling"]
        return None if value is None else int(value)

    def increment(self, by: int) -> list[DiffEntry]:
        """Add to the counter, refusing to cross the ceiling."""
        if by < 1:
            raise ToolRejected("invalid_amount", "by must be a positive integer")
        before = self.count
        after = before + by
        ceiling = self.ceiling
        if ceiling is not None and after > ceiling:
            raise ToolRejected(
                "ceiling_exceeded",
                f"the counter may not go above {ceiling}; it is at {before}",
            )
        self._store.data["count"] = after
        self._store.data["history"].append(
            {"at": self._clock.now().isoformat(), "by": by, "count": after}
        )
        return [DiffEntry(op="update", path="counter/count", before=before, after=after)]
