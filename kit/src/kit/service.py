"""Running a world's services deterministically.

Services are FastAPI apps with their own store. They are reached in-process
over ASGI during tests and CI (fast, no ports, no races) and over HTTP in the
docker-compose profile. Either way they are told the simulated time on every
request through :data:`SIM_TIME_HEADER` and never read a clock themselves.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Protocol

import httpx

from kit.clock import SimClock

SIM_TIME_HEADER = "X-Sim-Time"
"""Request header carrying the episode's current simulated time (ISO-8601, UTC)."""


class StateStore(Protocol):
    """The part of a service that can be snapshotted."""

    def snapshot(self) -> bytes:
        """Exact bytes of the store's contents."""
        ...

    def restore(self, blob: bytes) -> None:
        """Replace the contents with a snapshot."""
        ...


class JsonStore:
    """A dict-backed store with canonical JSON snapshots. For small services and examples."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data if data is not None else {}

    def snapshot(self) -> bytes:
        """Canonical JSON, so byte equality means state equality."""
        return json.dumps(self.data, sort_keys=True, separators=(",", ":")).encode()

    def restore(self, blob: bytes) -> None:
        """Replace the dict with the snapshot's contents."""
        self.data = json.loads(blob.decode())


class SqliteStore:
    """An in-memory SQLite store with exact snapshots.

    Uses ``Connection.serialize`` where the build supports it and falls back to
    a SQL dump, so a snapshot is always byte-identical for identical state.
    """

    def __init__(self, schema: str = "") -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if schema:
            self.connection.executescript(schema)

    @property
    def supports_serialize(self) -> bool:
        """Whether this Python's SQLite exposes serialize/deserialize."""
        return hasattr(self.connection, "serialize")

    def snapshot(self) -> bytes:
        """The database as bytes."""
        if self.supports_serialize:
            return bytes(self.connection.serialize())
        return "\n".join(self.connection.iterdump()).encode()

    def restore(self, blob: bytes) -> None:
        """Replace the database with a snapshot."""
        if self.supports_serialize:
            self.connection.deserialize(blob)
            return
        self.connection.executescript("PRAGMA writable_schema = 1;")
        tables = [
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            self.connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        self.connection.executescript(blob.decode())

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Rows as plain dicts, for graders and read-only endpoints."""
        return [dict(row) for row in self.connection.execute(sql, params)]

    def close(self) -> None:
        """Close the connection."""
        self.connection.close()


class AsgiService:
    """A world service mounted in-process, implementing :class:`kit.world.WorldState`.

    The service's app must read simulated time from :data:`SIM_TIME_HEADER`.
    """

    def __init__(self, name: str, app: Any, store: StateStore, clock: SimClock) -> None:
        self.name = name
        self.app = app
        self.store = store
        self.clock = clock
        self.base_url = f"http://{name}.sandbox.test"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Call the service with the current simulated time attached."""
        request_headers = {SIM_TIME_HEADER: self.clock.now().isoformat()}
        if headers is not None:
            request_headers = headers
        return self._send(method, path, json_body=json_body, params=params, headers=request_headers)

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None,
        params: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Drive the ASGI app from synchronous code.

        httpx 0.28 removed sync ASGI support, so each call runs the app on its
        own event loop. Episodes are single-threaded and synchronous, which is
        what keeps a run reproducible, so this stays simple on purpose.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:  # pragma: no cover - episodes never run inside a loop
            msg = "AsgiService must be called from synchronous code"
            raise RuntimeError(msg)

        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url=self.base_url, timeout=30.0
            ) as client:
                return await client.request(
                    method, path, json=json_body, params=params, headers=headers
                )

        return asyncio.run(call())

    def get(self, path: str, **params: Any) -> Any:
        """GET a path and return the decoded body."""
        response = self.request("GET", path, params=params or None)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        """POST a body. Callers inspect the status, because refusals are data."""
        return self.request("POST", path, json_body=body or {})

    def query(self, resource: str, /, **params: Any) -> Any:
        """Read-only admin surface used by graders: ``GET /admin/<resource>``."""
        return self.get(f"/admin/{resource.lstrip('/')}", **params)

    def snapshot(self) -> bytes:
        """Snapshot the underlying store."""
        return self.store.snapshot()

    def restore(self, blob: bytes) -> None:
        """Restore the underlying store."""
        self.store.restore(blob)

    def close(self) -> None:
        """Nothing to release: each request runs on its own loop and client."""

    def __repr__(self) -> str:
        return f"AsgiService({self.name!r})"


def sim_time_from_headers(headers: Any) -> str:
    """Read the simulated time a service was given, or fail loudly.

    A service that cannot see simulated time must not guess: guessing is how a
    wall clock sneaks into a deterministic run.
    """
    value = headers.get(SIM_TIME_HEADER)
    if not value:
        msg = f"request is missing {SIM_TIME_HEADER}; services must be called through the episode"
        raise ValueError(msg)
    return str(value)
