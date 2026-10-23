"""The rails' REST surface: a thin FastAPI layer over :class:`~.core.Rails`.

These endpoints are internal. The agent never calls them: Phase 2's tools wrap
them and decide what the agent sees (a timed-out payment comes back here with
``timed_out: true`` and the tool reports a timeout). ``/admin/*`` is for graders.

Every route is ``async def`` on purpose. FastAPI runs plain ``def`` routes in a
worker thread, and the episode's in-memory SQLite connection may only be used
from the thread that created it.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..common import install_error_handlers, sim_now, written
from .core import Rails
from .models import (
    EventRequest,
    MandateRequest,
    PaymentRequest,
    RailsConfig,
    RefundRequest,
    Rejection,
)

HTTP_STATUS: dict[str, int] = {
    Rejection.ACCOUNT_NOT_FOUND: 404,
    Rejection.MANDATE_NOT_FOUND: 404,
    Rejection.PAYMENT_NOT_FOUND: 404,
    Rejection.REFUND_NOT_FOUND: 404,
    Rejection.PAYER_NOT_FOUND: 404,
    Rejection.UNKNOWN_RESOURCE: 404,
    Rejection.INSUFFICIENT_FUNDS: 409,
    Rejection.IDEMPOTENCY_CONFLICT: 409,
    Rejection.INVALID_TRANSITION: 409,
    Rejection.ALREADY_LOADED: 409,
}
"""Every other rejection is a 422."""


class MandateAction(StrEnum):
    """What a payer can do to a mandate through the API."""

    PAUSE = "pause"
    RESUME = "resume"
    REVOKE = "revoke"


Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(rails: Rails) -> FastAPI:
    """Build the rails app around one episode's :class:`~.core.Rails`."""
    app = FastAPI(title="sandbox rails", docs_url=None, redoc_url=None)

    install_error_handlers(app, HTTP_STATUS)

    # ------------------------------------------------------- episode only
    @app.post("/internal/load")
    async def load(config: RailsConfig, now: Now) -> JSONResponse:
        return written(rails.load(config, now))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return written(rails.apply_event(body.name, body.payload, now))

    # ----------------------------------------------------------- payments
    @app.post("/payments")
    async def create_payment(body: PaymentRequest, now: Now) -> JSONResponse:
        return written(rails.create_payment(body, now))

    @app.get("/payments")
    async def payment_by_key(idempotency_key: str, now: Now) -> dict[str, Any]:
        return rails.payment_by_key(idempotency_key, now)

    @app.get("/payments/{payment_id}")
    async def payment(payment_id: str, now: Now) -> dict[str, Any]:
        return rails.payment(payment_id, now)

    @app.post("/refunds")
    async def create_refund(body: RefundRequest, now: Now) -> JSONResponse:
        return written(rails.create_refund(body, now))

    @app.get("/refunds/{refund_id}")
    async def refund(refund_id: str, now: Now) -> dict[str, Any]:
        return rails.refund(refund_id)

    # ------------------------------------------- accounts, limits, mandates
    @app.get("/accounts/{handle}")
    async def account(handle: str, now: Now) -> dict[str, Any]:
        return rails.account(handle)

    @app.get("/accounts/{handle}/transactions")
    async def transactions(
        handle: str, now: Now, limit: int = 20, status: str | None = None
    ) -> list[dict[str, Any]]:
        return rails.transactions(handle, now, limit=limit, status=status)

    @app.get("/accounts/{handle}/mandates")
    async def account_mandates(handle: str, now: Now) -> list[dict[str, Any]]:
        return rails.mandates_of(handle, now)

    @app.get("/handles/{handle}")
    async def lookup_handle(handle: str, now: Now) -> dict[str, Any]:
        return rails.lookup_handle(handle)

    @app.get("/limits/{handle}")
    async def limits(handle: str, now: Now) -> dict[str, Any]:
        return rails.limits(handle, now)

    @app.post("/mandates")
    async def create_mandate(body: MandateRequest, now: Now) -> JSONResponse:
        return written(rails.create_mandate(body, now))

    @app.get("/mandates/{mandate_id}")
    async def mandate(mandate_id: str, now: Now) -> dict[str, Any]:
        return rails.mandate(mandate_id, now)

    @app.post("/mandates/{mandate_id}/{action}")
    async def mandate_action(mandate_id: str, action: MandateAction, now: Now) -> JSONResponse:
        return written(rails.mandate_action(mandate_id, action.value, now))

    # -------------------------------------------------------- graders only
    @app.get("/admin/{resource}")
    async def admin(resource: str, request: Request, now: Now) -> Any:
        return rails.admin(resource, now, dict(request.query_params))

    return app


__all__ = ["MandateAction", "create_app"]
