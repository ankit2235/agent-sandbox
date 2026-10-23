"""Disputes' REST surface: a thin FastAPI layer over :class:`~.core.Disputes`.

Internal, like the rails': the agent reaches it only through the tools.
``/admin/*`` is for graders. Every route is ``async def`` for the reason given
in the rails' ``app.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..common import install_error_handlers, sim_now, written
from .core import Disputes
from .models import (
    DisputeCreate,
    EventRequest,
    EvidenceCreate,
    LoadRequest,
    MessageCreate,
    NoticeBatch,
    RefundOutcome,
    RefundRequestCreate,
    Rejection,
)

HTTP_STATUS: dict[str, int] = {
    Rejection.MERCHANT_NOT_FOUND: 404,
    Rejection.DISPUTE_NOT_FOUND: 404,
    Rejection.REFUND_REQUEST_NOT_FOUND: 404,
    Rejection.MESSAGE_NOT_FOUND: 404,
    Rejection.UNKNOWN_RESOURCE: 404,
    Rejection.REFUND_REQUEST_OPEN: 409,
    Rejection.DISPUTE_EXISTS: 409,
    Rejection.DISPUTE_WINDOW_CLOSED: 409,
    Rejection.EVIDENCE_WINDOW_CLOSED: 409,
    Rejection.ALREADY_LOADED: 409,
}
"""Every other refusal is a 422."""

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(disputes: Disputes) -> FastAPI:
    """Build the disputes app around one episode's :class:`~.core.Disputes`."""
    app = FastAPI(title="sandbox disputes", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    # ------------------------------------------------------- episode only
    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(disputes.load(body, now))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return written(disputes.apply_event(body.name, body.payload, now))

    @app.get("/internal/event-subject")
    async def event_subject(name: str, now: Now, request_id: str = "", dispute_id: str = "") -> Any:
        return disputes.subject(name, {"request_id": request_id, "dispute_id": dispute_id})

    @app.post("/internal/notices")
    async def notices(body: NoticeBatch, now: Now) -> JSONResponse:
        return written(disputes.add_notices(body, now))

    @app.post("/internal/refund-outcome")
    async def refund_outcome(body: RefundOutcome, now: Now) -> JSONResponse:
        return written(disputes.record_refund(body, now))

    # ------------------------------------------------------ refund requests
    @app.post("/refund-requests")
    async def create_refund_request(body: RefundRequestCreate, now: Now) -> JSONResponse:
        return written(disputes.create_refund_request(body, now))

    @app.get("/orders/{order_id}/refund-requests")
    async def refund_requests(order_id: str, now: Now) -> list[dict[str, Any]]:
        return disputes.refund_requests(order_id)

    # ------------------------------------------------------------ disputes
    @app.post("/disputes")
    async def create_dispute(body: DisputeCreate, now: Now) -> JSONResponse:
        return written(disputes.create_dispute(body, now))

    @app.get("/disputes")
    async def disputes_for(
        now: Now, order_id: str | None = None, payment_id: str | None = None
    ) -> list[dict[str, Any]]:
        return disputes.disputes_for(order_id, payment_id)

    @app.post("/disputes/{dispute_id}/evidence")
    async def add_evidence(dispute_id: str, body: EvidenceCreate, now: Now) -> JSONResponse:
        return written(disputes.add_evidence(dispute_id, body, now))

    # --------------------------------------------------------------- inbox
    @app.post("/messages")
    async def send_message(body: MessageCreate, now: Now) -> JSONResponse:
        return written(disputes.send_message(body, now))

    @app.post("/inbox/read")
    async def read_inbox(now: Now, unread_only: bool = False) -> JSONResponse:
        return written(disputes.read_inbox(unread_only, now))

    # -------------------------------------------------------- graders only
    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return disputes.admin(resource)

    return app


__all__ = ["create_app"]
