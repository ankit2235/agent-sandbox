"""What the security world's five services share.

The security world's own copy of the plumbing the payments world also has
(decision 86): worlds may not import each other, and the kit may not depend on
FastAPI. A later kit change may merge the two.

- :class:`SqlCore`: one SQLite store, all-or-nothing transactions, small helpers;
- :class:`Result`: what a changing operation did (body, ``state_diff``, events);
- :func:`install_error_handlers`: refusals as ``{"error": {"code", "message"}}``;
- :class:`WorldService`: a service mounted in-process that schedules the events
  it asks for and turns refusals back into :class:`kit.errors.ToolRejected`.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kit.clock import ScheduledEvent, SimClock
from kit.errors import ToolRejected
from kit.rng import SeededRNG
from kit.service import AsgiService, SqliteStore, sim_time_from_headers
from kit.trace import DiffEntry

Row = dict[str, Any]

INVALID_REQUEST = "invalid_request"
UNKNOWN_RESOURCE = "unknown_resource"
ALREADY_LOADED = "already_loaded"


# ---------------------------------------------------------------------- time
def iso(moment: dt.datetime) -> str:
    """Fixed-width UTC text to the second, so string order is time order."""
    if moment.tzinfo is None:
        msg = "timestamps must be timezone-aware"
        raise ValueError(msg)
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> dt.datetime:
    """Read a stored timestamp back."""
    return dt.datetime.fromisoformat(value)


def sim_now(request: Request) -> dt.datetime:
    """The simulated time the episode sent. A request without it fails loudly."""
    return dt.datetime.fromisoformat(sim_time_from_headers(request.headers))


def dumps(value: Any) -> str:
    """Canonical JSON for list and dict columns."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# -------------------------------------------------------------------- result
@dataclass(slots=True)
class Result:
    """What a changing operation did: the body, the rows it touched, and what to schedule."""

    body: dict[str, Any]
    state_diff: list[DiffEntry] = field(default_factory=list)
    scheduled: list[ScheduledEvent] = field(default_factory=list)
    created: bool = False

    def to_json(self) -> dict[str, Any]:
        """The response body, with the diff and the events in JSON form."""
        return {
            **self.body,
            "state_diff": [entry.model_dump(mode="json") for entry in self.state_diff],
            "scheduled_events": [event.model_dump(mode="json") for event in self.scheduled],
        }


def written(result: Result) -> JSONResponse:
    """201 when something new was recorded, 200 otherwise."""
    return JSONResponse(status_code=201 if result.created else 200, content=result.to_json())


# ----------------------------------------------------------------- rules core
class SqlCore:
    """One service's rules over its own SQLite database."""

    service = "service"
    """Prefix of every ``state_diff`` path this service writes."""

    def __init__(self, rng: SeededRNG, schema: str) -> None:
        self.store = SqliteStore(schema)
        self._rng = rng

    @property
    def _db(self) -> Any:
        return self.store.connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """All of an operation's writes happen, or none do."""
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Row | None:
        rows = self.store.query_all(sql, params)
        return rows[0] if rows else None

    def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self._db.execute(sql, params).fetchone()[0]

    def _insert(self, table: str, row: Row) -> None:
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._db.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))

    def _next_id(self, prefix: str, table: str) -> str:
        """A seeded id keyed by row count, so ids depend only on the seed and the database."""
        count = int(self._scalar(f"SELECT COUNT(*) FROM {table}")) + 1
        return f"{prefix}_{self._rng.child(f'{table}/{count}').token(12)}"

    def _created(self, table: str, row: Row) -> DiffEntry:
        return DiffEntry(op="create", path=f"{self.service}/{table}/{row['id']}", after=dict(row))

    def _set(self, table: str, row: Row, **changes: Any) -> list[DiffEntry]:
        """Update columns of one row by id and report each change. Column names are ours."""
        diff: list[DiffEntry] = []
        for column, value in changes.items():
            if row.get(column) == value:
                continue
            self._db.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row["id"]))
            diff.append(
                DiffEntry(
                    op="update",
                    path=f"{self.service}/{table}/{row['id']}/{column}",
                    before=row.get(column),
                    after=value,
                )
            )
            row[column] = value
        return diff

    def _loaded(self) -> bool:
        return bool(self._scalar("SELECT COUNT(*) FROM meta"))

    def _refuse_reload(self) -> None:
        if self._loaded():
            msg = f"the {self.service} service is already loaded"
            raise ToolRejected(ALREADY_LOADED, msg)


# ----------------------------------------------------------------------- http
def install_error_handlers(app: FastAPI, statuses: Mapping[str, int]) -> None:
    """Refusals become ``{"error": {...}}`` with a status from ``statuses`` (default 422)."""

    @app.exception_handler(ToolRejected)
    async def _rejected(_: Request, exc: ToolRejected) -> JSONResponse:
        return JSONResponse(
            status_code=statuses.get(exc.code, 422),
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={"error": {"code": INVALID_REQUEST, "message": problems}},
        )


class WorldService(AsgiService):
    """A security service as the episode holds it.

    Snapshot and restore come from :class:`kit.service.AsgiService`. A ``POST``
    that succeeds may carry ``scheduled_events``; they go straight onto the
    episode's clock, so a service decides its own deadlines.
    """

    def __init__(self, name: str, app: Any, store: SqliteStore, clock: SimClock) -> None:
        super().__init__(name, app, store, clock)
        self.event_prefix = f"{name}."

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Call the service, then put any events it asked for on the episode's clock."""
        response = super().request(
            method, path, json_body=json_body, params=params, headers=headers
        )
        if method.upper() == "POST" and response.is_success:
            for raw in response.json().get("scheduled_events", []):
                self.clock.schedule(ScheduledEvent.model_validate(raw))
        return response

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Call the service for a tool: a refusal comes back as :class:`ToolRejected`.

        A path with a ``.`` or ``..`` segment is refused before it is sent: the URL
        layer would resolve it, and a tool must never reach ``/admin`` or
        ``/internal`` through an id the agent chose (payments decision 81).
        """
        if any(segment in (".", "..") for segment in path.split("/")):
            msg = f"invalid path {path!r}"
            raise ToolRejected(INVALID_REQUEST, msg)
        clean = None if params is None else {k: v for k, v in params.items() if v is not None}
        if method.upper() == "GET":
            response = self.request("GET", path, params=clean or None)
        else:
            response = self.request(method, path, json_body=body or {}, params=clean or None)
        payload = response.json()
        if not response.is_success:
            error = payload["error"]
            raise ToolRejected(str(error["code"]), str(error["message"]))
        return payload

    def load(self, config: Mapping[str, Any]) -> Any:
        """Load this service's part of a task."""
        response = self.post("/internal/load", dict(config))
        if response.status_code not in (200, 201):
            error = response.json()["error"]
            msg = f"the {self.name} service refused the task's initial state: {error['message']}"
            raise ValueError(msg)
        return response.json()

    def handles(self, event: ScheduledEvent) -> bool:
        """Whether a scheduled event belongs to this service."""
        return event.name.startswith(self.event_prefix)

    def deliver(
        self, event: ScheduledEvent, extra: Mapping[str, Any] | None = None
    ) -> list[DiffEntry]:
        """Deliver a due event; ``extra`` carries facts from other services. Returns the diff."""
        payload = {**event.payload, **(extra or {})}
        response = self.post("/internal/events", {"name": event.name, "payload": payload})
        if not response.is_success:
            # An event a service refuses is a bug in the task or the world, never agent error.
            msg = (
                f"the {self.name} service refused event {event.name!r}: "
                f"{response.json()['error']['message']}"
            )
            raise RuntimeError(msg)
        return diff_of(response.json())


def diff_of(body: Mapping[str, Any]) -> list[DiffEntry]:
    """The ``state_diff`` a service returned, as kit diff entries."""
    return [DiffEntry.model_validate(entry) for entry in body.get("state_diff", [])]


__all__ = [
    "ALREADY_LOADED",
    "INVALID_REQUEST",
    "UNKNOWN_RESOURCE",
    "Result",
    "Row",
    "SqlCore",
    "WorldService",
    "diff_of",
    "dumps",
    "install_error_handlers",
    "iso",
    "parse_time",
    "sim_now",
    "written",
]
