"""Commerce's REST surface: a thin FastAPI layer over :class:`~.core.Commerce`.

Internal, like the rails': the agent reaches commerce only through the tools.
``/admin/*`` is for graders. Every route is ``async def`` for the reason given
in the rails' ``app.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..common import install_error_handlers, sim_now, written
from .core import Commerce
from .models import (
    CartLine,
    DealMove,
    EventRequest,
    InvoiceCreate,
    InvoiceDispute,
    InvoiceWithdraw,
    LoadRequest,
    OfferRequest,
    PaymentLink,
    PaymentStatusUpdate,
    Rejection,
    SubscriptionChange,
)

HTTP_STATUS: dict[str, int] = {
    Rejection.MERCHANT_NOT_FOUND: 404,
    Rejection.PRODUCT_NOT_FOUND: 404,
    Rejection.OFFER_NOT_FOUND: 404,
    Rejection.ORDER_NOT_FOUND: 404,
    Rejection.SUBSCRIPTION_NOT_FOUND: 404,
    Rejection.PLAN_NOT_FOUND: 404,
    Rejection.PAGE_NOT_FOUND: 404,
    Rejection.UNKNOWN_RESOURCE: 404,
    Rejection.OUT_OF_STOCK: 409,
    Rejection.ORDER_ALREADY_PAID: 409,
    Rejection.ORDER_NOT_PAYABLE: 409,
    Rejection.INVALID_TRANSITION: 409,
    Rejection.ALREADY_LOADED: 409,
    Rejection.DEAL_NOT_FOUND: 404,
    Rejection.INVOICE_NOT_FOUND: 404,
    Rejection.DEAL_NOT_OPEN: 409,
    Rejection.DEAL_NOT_AGREED: 409,
    Rejection.NO_STANDING_OFFER: 409,
    Rejection.INVOICE_NOT_OPEN: 409,
}
"""Every other refusal is a 422."""

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(commerce: Commerce) -> FastAPI:
    """Build the commerce app around one episode's :class:`~.core.Commerce`."""
    app = FastAPI(title="sandbox commerce", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    # ------------------------------------------------------- episode only
    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(commerce.load(body, now))

    @app.post("/internal/events")
    async def event(body: EventRequest, now: Now) -> JSONResponse:
        return written(commerce.apply_event(body.name, body.payload, now))

    @app.post("/internal/payment-status")
    async def payment_status(body: PaymentStatusUpdate, now: Now) -> JSONResponse:
        return written(commerce.payment_status(body, now))

    # ----------------------------------------------------- catalogue, pages
    @app.get("/products")
    async def search(
        now: Now,
        query: str | None = None,
        merchant_id: str | None = None,
        category: str | None = None,
        max_price_paise: int | None = None,
    ) -> list[dict[str, Any]]:
        return commerce.search(query, merchant_id, category, max_price_paise)

    @app.get("/merchants")
    async def merchant_by_handle(handle: str, now: Now) -> dict[str, Any]:
        return commerce.merchant_by_handle(handle)

    @app.get("/merchants/{merchant_id}")
    async def merchant(merchant_id: str, now: Now) -> dict[str, Any]:
        return commerce.merchant(merchant_id)

    @app.get("/merchants/{merchant_id}/pages/{page:path}")
    async def page(merchant_id: str, page: str, now: Now) -> dict[str, Any]:
        return commerce.page(merchant_id, page)

    # ---------------------------------------------------- carts and orders
    @app.get("/merchants/{merchant_id}/cart")
    async def cart(merchant_id: str, now: Now) -> dict[str, Any]:
        return commerce.cart(merchant_id, now)

    @app.post("/merchants/{merchant_id}/cart/lines")
    async def set_line(merchant_id: str, body: CartLine, now: Now) -> JSONResponse:
        return written(commerce.set_cart_line(merchant_id, body, now))

    @app.post("/merchants/{merchant_id}/cart/offer")
    async def apply_offer(merchant_id: str, body: OfferRequest, now: Now) -> JSONResponse:
        return written(commerce.apply_offer(merchant_id, body.code, now))

    @app.post("/merchants/{merchant_id}/checkout")
    async def checkout(merchant_id: str, now: Now) -> JSONResponse:
        return written(commerce.checkout(merchant_id, now))

    @app.get("/orders/{order_id}")
    async def order(order_id: str, now: Now) -> dict[str, Any]:
        return commerce.order(order_id)

    @app.get("/orders/{order_id}/payable")
    async def payable(
        order_id: str, payee: str, amount_paise: int, now: Now, mandate_id: str | None = None
    ) -> dict[str, Any]:
        return commerce.payable(order_id, payee, amount_paise, mandate_id, now)

    @app.post("/orders/{order_id}/payments")
    async def link_payment(order_id: str, body: PaymentLink, now: Now) -> JSONResponse:
        return written(commerce.link_payment(order_id, body, now))

    @app.get("/payments/{payment_id}/order")
    async def order_for_payment(payment_id: str, now: Now) -> dict[str, Any]:
        return commerce.order_for_payment(payment_id)

    # -------------------------------------------------------- subscriptions
    @app.post("/subscriptions/{subscription_id}")
    async def change_subscription(
        subscription_id: str, body: SubscriptionChange, now: Now
    ) -> JSONResponse:
        return written(commerce.change_subscription(subscription_id, body, now))

    # ---------------------------------------------------------------- deals
    @app.get("/deals")
    async def deals(now: Now, merchant_id: str | None = None) -> list[dict[str, Any]]:
        return commerce.deals(merchant_id)

    @app.post("/deals/{deal_id}/offers")
    async def deal_offer(deal_id: str, body: DealMove, now: Now) -> JSONResponse:
        return written(commerce.deal_offer(deal_id, body, now))

    @app.post("/deals/{deal_id}/accept")
    async def deal_accept(deal_id: str, body: DealMove, now: Now) -> JSONResponse:
        return written(commerce.deal_accept(deal_id, body, now))

    @app.post("/deals/{deal_id}/messages")
    async def deal_message(deal_id: str, body: DealMove, now: Now) -> JSONResponse:
        return written(commerce.deal_message(deal_id, body, now))

    @app.post("/deals/{deal_id}/invoices")
    async def invoice(deal_id: str, body: InvoiceCreate, now: Now) -> JSONResponse:
        return written(commerce.invoice_create(deal_id, body, now))

    @app.post("/invoices/{order_id}/withdraw")
    async def withdraw(order_id: str, body: InvoiceWithdraw, now: Now) -> JSONResponse:
        return written(commerce.invoice_withdraw(order_id, body, now))

    @app.post("/invoices/{order_id}/disputes")
    async def dispute(order_id: str, body: InvoiceDispute, now: Now) -> JSONResponse:
        return written(commerce.invoice_dispute(order_id, body, now))

    # -------------------------------------------------------- graders only
    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return commerce.admin(resource)

    return app


__all__ = ["create_app"]
