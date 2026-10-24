"""The response service's REST surface. Internal: the agent reaches it only through the tools."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..common import ALREADY_LOADED, UNKNOWN_RESOURCE, install_error_handlers, sim_now, written
from .core import (
    ACTION_NOT_FOUND,
    ALREADY_BLOCKED,
    ALREADY_DISABLED,
    ALREADY_ISOLATED,
    ALREADY_RESTORED,
    NEEDS_APPROVAL,
    NO_SUCH_HOST,
    ActionRequest,
    LoadRequest,
    Response,
    RestoreRequest,
)

HTTP_STATUS = {
    ACTION_NOT_FOUND: 404,
    NO_SUCH_HOST: 404,
    UNKNOWN_RESOURCE: 404,
    NEEDS_APPROVAL: 403,
    ALREADY_ISOLATED: 409,
    ALREADY_DISABLED: 409,
    ALREADY_BLOCKED: 409,
    ALREADY_RESTORED: 409,
    ALREADY_LOADED: 409,
}

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(response: Response) -> FastAPI:
    """Build the response app around one episode's :class:`~.core.Response`."""
    app = FastAPI(title="sandbox response", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(response.load(body))

    @app.get("/internal/containment")
    async def containment(now: Now) -> dict[str, list[str]]:
        return response.containment()

    @app.post("/actions")
    async def act(body: ActionRequest, now: Now) -> JSONResponse:
        return written(response.act(body, now))

    @app.post("/actions/{action_id}/restore")
    async def restore(action_id: str, body: RestoreRequest, now: Now) -> JSONResponse:
        return written(response.restore(action_id, body, now))

    @app.get("/history")
    async def history(target: str, now: Now) -> list[dict[str, Any]]:
        return response.history(target)

    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return response.admin(resource)

    return app


__all__ = ["create_app"]
