"""The alerts service's REST surface. Internal: the agent reaches it only through the tools."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..common import ALREADY_LOADED, UNKNOWN_RESOURCE, install_error_handlers, sim_now, written
from .core import ALERT_NOT_FOUND, Alerts, EventRequest, LoadRequest

HTTP_STATUS = {ALERT_NOT_FOUND: 404, UNKNOWN_RESOURCE: 404, ALREADY_LOADED: 409}

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(alerts: Alerts) -> FastAPI:
    """Build the alerts app around one episode's :class:`~.core.Alerts`."""
    app = FastAPI(title="sandbox alerts", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(alerts.load(body))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return written(alerts.apply_event(body))

    @app.get("/alerts")
    async def queue(
        now: Now, severity: str | None = None, ticket_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return alerts.alerts(now, severity=severity, ticket_id=ticket_id, limit=limit)

    @app.get("/alerts/{alert_id}")
    async def alert(alert_id: str, now: Now) -> dict[str, Any]:
        return alerts.alert(alert_id, now)

    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return alerts.admin(resource)

    return app


__all__ = ["create_app"]
