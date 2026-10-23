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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kit.errors import ToolRejected
from kit.service import sim_time_from_headers

from .core import Rails, Result
from .models import EventRequest, PaymentRequest, RailsConfig, RefundRequest, Rejection

HTTP_STATUS = {
    Rejection.ACCOUNT_NOT_FOUND: 404,
    Rejection.MANDATE_NOT_FOUND: 404,
    Rejection.PAYMENT_NOT_FOUND: 404,
    Rejection.REFUND_NOT_FOUND: 404,
    Rejection.PAYER_NOT_FOUND: 404,
    Rejection.UNKNOWN_RESOURCE: 404,
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


def sim_now(request: Request) -> dt.datetime:
    """The simulated time the episode sent. A request without it fails loudly."""
    return dt.datetime.fromisoformat(sim_time_from_headers(request.headers))


Now = Annotated[dt.datetime, Depends(sim_now)]


def _written(result: Result) -> JSONResponse:
    """201 when something new was recorded, 200 for a replay or an update."""
    return JSONResponse(status_code=201 if result.created else 200, content=result.to_json())


def create_app(rails: Rails) -> FastAPI:
    """Build the rails app around one episode's :class:`~.core.Rails`."""
    app = FastAPI(title="sandbox rails", docs_url=None, redoc_url=None)

    @app.exception_handler(ToolRejected)
    async def _rejected(_: Request, exc: ToolRejected) -> JSONResponse:
        status = HTTP_STATUS.get(Rejection(exc.code), 422)
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={"error": {"code": Rejection.INVALID_REQUEST.value, "message": problems}},
        )

    # ------------------------------------------------------- episode only
    @app.post("/internal/load")
    async def load(config: RailsConfig, now: Now) -> JSONResponse:
        return _written(rails.load(config, now))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return _written(rails.apply_event(body.name, body.payload, now))

    # ----------------------------------------------------------- payments
    @app.post("/payments")
    async def create_payment(body: PaymentRequest, now: Now) -> JSONResponse:
        return _written(rails.create_payment(body, now))

    @app.get("/payments")
    async def payment_by_key(idempotency_key: str, now: Now) -> dict[str, Any]:
        return rails.payment_by_key(idempotency_key, now)

    @app.get("/payments/{payment_id}")
    async def payment(payment_id: str, now: Now) -> dict[str, Any]:
        return rails.payment(payment_id, now)

    @app.post("/refunds")
    async def create_refund(body: RefundRequest, now: Now) -> JSONResponse:
        return _written(rails.create_refund(body, now))

    @app.get("/refunds/{refund_id}")
    async def refund(refund_id: str, now: Now) -> dict[str, Any]:
        return rails.refund(refund_id)

    # ------------------------------------------- accounts, limits, mandates
    @app.get("/accounts/{handle}")
    async def account(handle: str, now: Now) -> dict[str, Any]:
        return rails.account(handle)

    @app.get("/handles/{handle}")
    async def lookup_handle(handle: str, now: Now) -> dict[str, Any]:
        return rails.lookup_handle(handle)

    @app.get("/limits/{handle}")
    async def limits(handle: str, now: Now) -> dict[str, Any]:
        return rails.limits(handle, now)

    @app.get("/mandates/{mandate_id}")
    async def mandate(mandate_id: str, now: Now) -> dict[str, Any]:
        return rails.mandate(mandate_id, now)

    @app.post("/mandates/{mandate_id}/{action}")
    async def mandate_action(mandate_id: str, action: MandateAction, now: Now) -> JSONResponse:
        return _written(rails.mandate_action(mandate_id, action.value, now))

    # -------------------------------------------------------- graders only
    @app.get("/admin/{resource}")
    async def admin(resource: str, request: Request, now: Now) -> Any:
        return rails.admin(resource, now, dict(request.query_params))

    return app


__all__ = ["MandateAction", "create_app", "sim_now"]
