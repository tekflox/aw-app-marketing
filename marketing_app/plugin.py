"""Entrypoint referenced by aw-app.json's ``runtime.entrypoint``
(``marketing_app.plugin:MarketingAppPlugin``).

Tier-1 (in-process) app on the F4 framework ``ctx`` facades:

* ``ctx.routes`` (``routes:register``) — the sub-app from ``routes.py``, mounted
  by the runtime at ``/api/apps/marketing``.
* ``ctx.secrets`` (``secrets:own``) — the Meta access token, encrypted at rest,
  read/written by ``routes.py``'s ``/settings``/``/logout``. Never ``ctx.config``
  — see ``config.py``'s module docstring for why.
* ``fs:workspace-data`` — the campaign ledger under
  ``<AW_WORKSPACE_HOME>/data/marketing`` (``ledger.py``). No facade to call; the
  permission is what authorises writing there.
* ``agents:contribute`` — the ``marketing-sonnet`` agent and its config, seeded
  from the manifest by the runtime, not by code here.

**``activate``, ``on_config_saved``, and ``routes.py``'s ``/settings``/
``/logout`` all write ``mcp.json``, and together they are the only writers of
it.** That is the one genuinely delicate thing in this app, and its failure
mode is silent — see ``mcp/self_register.py`` for the full reasoning and the
``runtime.py``/``routes.py`` line references that make it necessary.
Activation re-runs on every boot and after every update, so the file (and the
Meta token inside it, which lives in the encrypted secret store, not in this
repo) is rebuilt from ``ctx.secrets`` each time rather than having to survive
anything.
"""

from __future__ import annotations

import logging
import os

from . import config as cfg
from . import routes as routes_mod
from .ledger import Ledger
from .mcp import self_register as mcp_self_register

log = logging.getLogger("aw_apps.marketing")


class MarketingAppPlugin:
    def _port(self) -> int:
        return int(os.environ.get("AW_PORT") or 9030)

    async def activate(self, ctx) -> None:
        self.ctx = ctx
        self.ledger = Ledger()

        ctx.routes.register(routes_mod.build_routes(ctx, self.ledger))

        self._write_mcp_json(ctx)

        readiness = cfg.readiness(getattr(ctx, "config", {}), ctx.secrets.read(routes_mod.SECRET_KEY))
        log.info("aw-app-marketing activated — %s", readiness["detail"])

    async def on_config_saved(self, ctx) -> None:
        """Rewrite ``mcp.json`` on every config save.

        The Meta token itself no longer changes here (it travels through
        ``routes.py``'s ``/settings``/``/logout`` instead, straight to
        ``ctx.secrets``) — this is a no-op rewrite in the common case, kept
        because none of the OTHER config fields (ad account, budget, ...)
        should ever silently stop this hook from running if that changes.
        """
        self.ctx = ctx
        changed = self._write_mcp_json(ctx)
        log.info("aw-app-marketing config saved — mcp.json %s; %s",
                 "rewritten" if changed else "unchanged",
                 cfg.readiness(getattr(ctx, "config", {}), ctx.secrets.read(routes_mod.SECRET_KEY))["detail"])

    def _write_mcp_json(self, ctx) -> bool:
        return mcp_self_register.register_self(
            ctx.package_dir, self._port(), ctx.secrets.read(routes_mod.SECRET_KEY))

    async def deactivate(self) -> None:
        log.info("aw-app-marketing deactivated")
