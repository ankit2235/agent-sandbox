"""The telemetry service's REST surface. Internal: the agent reaches it only through the tools."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from ..common import ALREADY_LOADED, UNKNOWN_RESOURCE, install_error_handlers, sim_now, written
from .core import EventRequest, Filters, LoadRequest, Telemetry

HTTP_STATUS = {UNKNOWN_RESOURCE: 404, ALREADY_LOADED: 409}

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(telemetry: Telemetry) -> FastAPI:
    """Build the telemetry app around one episode's :class:`~.core.Telemetry`."""
    app = FastAPI(title="sandbox telemetry", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(telemetry.load(body))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return written(telemetry.apply_event(body))

    @app.get("/events")
    async def events(
        now: Now,
        source: str | None = None,
        host: str | None = None,
        user: str | None = None,
        ip: str | None = None,
        domain: str | None = None,
        process: str | None = None,
        contains: str | None = None,
        entity: Annotated[list[str] | None, Query()] = None,
        event_id: Annotated[list[str] | None, Query()] = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        filters = Filters(
            source=source,
            host=host,
            user=user,
            ip=ip,
            domain=domain,
            process=process,
            contains=contains,
            entities=entity or [],
            ids=event_id or [],
            since=since,
            until=until,
            limit=limit,
        )
        return telemetry.search(filters, now)

    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return telemetry.admin(resource)

    return app


__all__ = ["create_app"]
