"""marketing_app's FastAPI sub-app, mounted by the runtime at
``/api/apps/marketing`` behind its ``IdentityGuard`` (ADR Decision 2/6 — apps
never implement their own auth in integrated mode).

Every path here is RELATIVE (no ``/api/apps/marketing`` prefix), matching the
shape the rest of the estate uses.

``meta_access_token`` is deliberately NOT routed through the generic
``POST /api/apps/marketing/config`` endpoint (which would land it in
``loaded.config`` — plain, cloud-synced app config; ``src/apps/routes.py``'s
``save_app_config`` has no concept of the schema's ``x-secret`` marker, so
nothing there would have stopped it). ``POST /settings`` here goes straight to
``ctx.secrets`` instead — same pattern as aw-app-notion's ``notion_token`` /
aw-app-git's ``github_token`` / aw-app-android-studio's ``remote_token``. The
``meta_access_token`` field stays in ``config_schema`` only so the generic
Settings UI knows to render it as a password input (``x-secret``); its value
is read and written here, never by the generic config path.

Endpoints:

* ``GET /status`` — is this app actually configured, and what is missing. Exists
  because this workspace's characteristic failure is silent degradation: an
  unconfigured app that answers 200 to everything looks identical to a working
  one. This makes "not configured" a thing you can read.
* ``POST /settings`` / ``POST /logout`` — write/clear the Meta token in the
  encrypted secret store, and rewrite ``mcp.json`` immediately so a pasted
  token takes effect on the very next tool call. These two are additional
  writers of ``mcp.json``, alongside ``plugin.py``'s ``activate``/
  ``on_config_saved`` — see ``mcp/self_register.py``'s module docstring.
* ``POST /mcp`` — the Streamable HTTP MCP endpoint aw-mcp-gateway's app-scan
  discovers via ``mcp.json`` (see ``mcp/self_register.py``). ``GET`` on it
  returns 405, same as the other apps' handlers, so a browser hitting the URL
  gets a clear answer rather than a stack trace.
"""

from __future__ import annotations

import os

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

from . import config as cfg
from .ledger import Ledger
from .mcp import self_register as mcp_self_register
from .mcp.http_handler import TOOLS_SCHEMA
from .mcp.http_handler import handle_request as mcp_handle_request

SECRET_KEY = "meta_access_token"


def build_routes(ctx, ledger: Ledger | None = None) -> FastAPI:
    """``ctx`` is held onto and re-read on every request, not snapshotted, so a
    token or config value saved later is picked up without re-registering
    anything."""
    api = FastAPI(title="marketing")
    store = ledger or Ledger()

    def _config() -> dict:
        return getattr(ctx, "config", {}) or {}

    def _token() -> str:
        return ctx.secrets.read(SECRET_KEY) or ""

    def _port() -> int:
        return int(os.environ.get("AW_PORT") or 9030)

    @api.get("/status")
    async def status() -> dict:
        readiness = cfg.readiness(_config(), _token())
        return {
            **readiness,
            "tools": [t["name"] for t in TOOLS_SCHEMA],
            "ledger_path": store.path,
        }

    @api.post("/settings")
    async def save_settings(data: dict = Body(...)) -> dict:
        token = (data.get(SECRET_KEY) or "").strip()
        if not token:
            return JSONResponse({"ok": False, "error": f"{SECRET_KEY} is required"},
                                status_code=400)
        ctx.secrets.write(SECRET_KEY, token)
        mcp_self_register.register_self(ctx.package_dir, _port(), token)
        return {"ok": True, "meta_upstream_enabled": True}

    @api.post("/logout")
    async def clear_token() -> dict:
        ctx.secrets.delete(SECRET_KEY)
        mcp_self_register.register_self(ctx.package_dir, _port(), "")
        return {"ok": True, "meta_upstream_enabled": False}

    @api.post("/mcp")
    async def mcp_post(data: dict | list = Body(...)):
        messages = data if isinstance(data, list) else [data]
        responses = []
        for message in messages:
            reply = await mcp_handle_request(
                message, config=_config(), token=_token(), ledger=store)
            if reply is not None:
                responses.append(reply)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses if isinstance(data, list) else responses[0])

    @api.get("/mcp")
    async def mcp_get():
        return Response(status_code=405)

    return api
