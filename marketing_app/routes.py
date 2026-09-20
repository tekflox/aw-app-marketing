"""marketing_app's FastAPI sub-app, mounted by the runtime at
``/api/apps/marketing`` behind its ``IdentityGuard`` (ADR Decision 2/6 — apps
never implement their own auth in integrated mode).

Every path here is RELATIVE (no ``/api/apps/marketing`` prefix), matching the
shape the rest of the estate uses.

Two endpoints:

* ``GET /status`` — is this app actually configured, and what is missing. Exists
  because this workspace's characteristic failure is silent degradation: an
  unconfigured app that answers 200 to everything looks identical to a working
  one. This makes "not configured" a thing you can read.
* ``POST /mcp`` — the Streamable HTTP MCP endpoint aw-mcp-gateway's app-scan
  discovers via ``mcp.json`` (see ``mcp/self_register.py``). ``GET`` on it
  returns 405, same as the other apps' handlers, so a browser hitting the URL
  gets a clear answer rather than a stack trace.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

from . import config as cfg
from .ledger import Ledger
from .mcp.http_handler import TOOLS_SCHEMA
from .mcp.http_handler import handle_request as mcp_handle_request


def build_routes(config_provider: Callable[[], dict[str, Any] | None],
                 ledger: Ledger | None = None) -> FastAPI:
    """``config_provider`` is a callable, not a dict, on purpose: the runtime
    hands ``ctx.config`` back refreshed on a config save, and a snapshot taken
    at activation would serve a token the user has since replaced."""
    api = FastAPI(title="marketing")
    store = ledger or Ledger()

    @api.get("/status")
    async def status() -> dict:
        readiness = cfg.readiness(config_provider())
        return {
            **readiness,
            "tools": [t["name"] for t in TOOLS_SCHEMA],
            "ledger_path": store.path,
        }

    @api.post("/mcp")
    async def mcp_post(data: dict | list = Body(...)):
        messages = data if isinstance(data, list) else [data]
        responses = []
        for message in messages:
            reply = await mcp_handle_request(
                message, config=config_provider(), ledger=store)
            if reply is not None:
                responses.append(reply)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses if isinstance(data, list) else responses[0])

    @api.get("/mcp")
    async def mcp_get():
        return Response(status_code=405)

    return api
