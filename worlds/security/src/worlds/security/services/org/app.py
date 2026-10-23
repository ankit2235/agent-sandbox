"""The org service's REST surface. Internal: the agent reaches it only through the tools.

Every route is ``async def``: FastAPI runs plain ``def`` routes in a worker
thread, and the episode's SQLite connection is used one call at a time.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..common import ALREADY_LOADED, UNKNOWN_RESOURCE, install_error_handlers, sim_now, written
from .core import HOST_NOT_FOUND, USER_NOT_FOUND, Directory, LoadRequest

HTTP_STATUS = {HOST_NOT_FOUND: 404, USER_NOT_FOUND: 404, UNKNOWN_RESOURCE: 404, ALREADY_LOADED: 409}

Now = Annotated[dt.datetime, Depends(sim_now)]


def create_app(directory: Directory) -> FastAPI:
    """Build the org app around one episode's :class:`~.core.Directory`."""
    app = FastAPI(title="sandbox org", docs_url=None, redoc_url=None)
    install_error_handlers(app, HTTP_STATUS)

    @app.post("/internal/load")
    async def load(body: LoadRequest, now: Now) -> JSONResponse:
        return written(directory.load(body))

    @app.get("/hosts/{host_id}")
    async def host(host_id: str, now: Now) -> dict[str, Any]:
        return directory.host(host_id)

    @app.get("/host-by-ip")
    async def host_by_ip(ip: str, now: Now) -> dict[str, Any] | None:
        return directory.host_by_ip(ip)

    @app.get("/users/{user_id}")
    async def user(user_id: str, now: Now) -> dict[str, Any]:
        return directory.user(user_id)

    @app.get("/business-indicator")
    async def business_indicator(kind: str, value: str, now: Now) -> dict[str, Any] | None:
        return directory.business_indicator(kind, value)

    @app.get("/admin/{resource}")
    async def admin(resource: str, now: Now) -> Any:
        return directory.admin(resource)

    return app


__all__ = ["create_app"]
