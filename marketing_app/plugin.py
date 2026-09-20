"""Entrypoint referenced by aw-app.json's ``runtime.entrypoint``
(``marketing_app.plugin:MarketingAppPlugin``).

Tier-1 (in-process) app on the F4 framework ``ctx`` facades:

* ``ctx.routes`` (``routes:register``) — the sub-app from ``routes.py``, mounted
  by the runtime at ``/api/apps/marketing``.
* ``fs:workspace-data`` — the campaign ledger under
  ``<AW_WORKSPACE_HOME>/data/marketing`` (``ledger.py``). No facade to call; the
  permission is what authorises writing there.
* ``agents:contribute`` — the ``marketing-sonnet`` agent and its config, seeded
  from the manifest by the runtime, not by code here.

**``activate`` and ``on_config_saved`` both write ``mcp.json``, and they are the
only writers of it.** That is the one genuinely delicate thing in this app, and
its failure mode is silent — see ``mcp/self_register.py`` for the full reasoning
and the ``runtime.py``/``routes.py`` line references that make it necessary.
Activation re-runs on every boot and after every update, so the file (and the
Meta token inside it, which lives in ``config_store``, not in this repo) is
rebuilt from config each time rather than having to survive anything.
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

        # ctx.config is re-read through this lambda rather than snapshotted, so
        # a token saved later is picked up without re-activating the app.
        ctx.routes.register(
            routes_mod.build_routes(lambda: getattr(self.ctx, "config", {}), self.ledger)
        )

        self._write_mcp_json(ctx)

        readiness = cfg.readiness(getattr(ctx, "config", {}))
        log.info("aw-app-marketing activated — %s", readiness["detail"])

    async def on_config_saved(self, ctx) -> None:
        """Rewrite ``mcp.json`` the moment a token is pasted.

        The runtime calls this BEFORE reloading the gateway
        (``src/apps/routes.py``), and ``contributes.mcp.reload_on_save`` in the
        manifest is what makes that reload happen at all — so saving a token
        takes effect immediately instead of on the next workspace boot.
        """
        self.ctx = ctx
        changed = self._write_mcp_json(ctx)
        log.info("aw-app-marketing config saved — mcp.json %s; %s",
                 "rewritten" if changed else "unchanged",
                 cfg.readiness(getattr(ctx, "config", {}))["detail"])

    def _write_mcp_json(self, ctx) -> bool:
        return mcp_self_register.register_self(
            ctx.package_dir, self._port(), getattr(ctx, "config", {}) or {})

    async def deactivate(self) -> None:
        log.info("aw-app-marketing deactivated")
