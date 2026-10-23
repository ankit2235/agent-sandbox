"""The tickets service's REST surface. Internal: the agent reaches it only through the tools."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..common import ALREADY_LOADED, UNKNOWN_RESOURCE, install_error_handlers, sim_now, written
from .core import (
    ALREADY_ESCALATED,
    TICKET_NOT_FOUND,
    Escalation,
    EventRequest,
    LoadRequest,
    ReportSubmission,
    Tickets,
    TicketUpdate,
)

HTTP_STATUS = {
    TICKET_NOT_FOUND: 404,
    UNKNOWN_RESOURCE: 404,
    ALREADY_ESCALATED: 409,
    ALREADY_LOADED: 409,
}

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(tickets: Tickets) -> FastAPI:
    """Build the tickets app around one episode's :class:`~.core.Tickets`."""
    app = FastAPI(title="sandbox tickets", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(tickets.load(body, now))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return written(tickets.apply_event(body, now))

    @app.get("/tickets/{ticket_id}")
    async def ticket(ticket_id: str, now: Now) -> dict[str, Any]:
        return tickets.ticket(ticket_id)

    @app.post("/tickets/{ticket_id}")
    async def update(ticket_id: str, body: TicketUpdate, now: Now) -> JSONResponse:
        return written(tickets.update(ticket_id, body, now))

    @app.post("/tickets/{ticket_id}/escalate")
    async def escalate(ticket_id: str, body: Escalation, now: Now) -> JSONResponse:
        return written(tickets.escalate(ticket_id, body, now))

    @app.post("/tickets/{ticket_id}/reports")
    async def report(ticket_id: str, body: ReportSubmission, now: Now) -> JSONResponse:
        return written(tickets.submit_report(ticket_id, body, now))

    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return tickets.admin(resource)

    return app


__all__ = ["create_app"]
