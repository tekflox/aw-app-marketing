"""The **single writer** of this app's ``mcp.json``.

aw-mcp-gateway discovers upstreams by scanning ``<installed-app-dir>/mcp.json``
(its ``scan_app_mcp_servers()``). This app needs TWO entries in that one file:

* ``marketing`` — our own ``POST /mcp`` endpoint (``http_handler.py``), whose
  URL is only knowable at runtime (``socket.gethostname()`` + ``$AW_PORT``);
* ``meta-ads`` — Meta's hosted Ads MCP server at ``https://mcp.facebook.com/ads``,
  authenticated with the access token from this app's own config.

Why we do NOT use ``mcp.template.json``
---------------------------------------
The workspace's normal answer for "an upstream that needs a per-install
credential" is ``mcp.template.json`` (aw-workspace ``src/apps/mcp_template.py``),
rendered by the runtime into ``mcp.json``. It cannot be combined with the
self-registration this app also needs, because ``mcp_template.render()``
**writes the whole file** (``_write(output_path(package_dir), rendered)``) —
it does not merge. And the runtime always renders **after** the plugin has run,
on both paths:

* activation: ``await plugin.activate(ctx)`` (``src/apps/runtime.py:902``)
  then ``self._render_mcp_template(loaded)`` (``src/apps/runtime.py:930``);
* config save: ``on_config_saved`` (``src/apps/routes.py:763-765``)
  then ``runtime._render_mcp_template(loaded)`` (``src/apps/routes.py:770``).

So shipping both mechanisms would produce an app that loses its own ``marketing``
entry on every boot and every config save — silently, with the gateway still
listing the upstream and simply serving nothing. Instead this module writes the
complete file, both entries at once, and the repo ships no template at all, so
``render()`` short-circuits on its ``os.path.isfile(src)`` guard and never
touches our output. ``aw-app-notion`` writes its own ``mcp.json`` the same way
(named as the precedent in ``mcp_template.py``'s own docstring). It is legitimate
here because we are Tier-1: the gap the template exists to close is Tier-2's,
where no workspace-side code runs at all.

MAINTENANCE NOTE for whoever evolves ``src/apps/mcp_template.py``: this app is
the exception that opts out. If the runtime ever learns to *merge* a rendered
template into an existing ``mcp.json``, this module should be deleted in favour
of the template — check ``runtime.py:930`` and ``routes.py:770`` still read as
described above before assuming otherwise.

Tier-1 vs Tier-2 addressing: a Tier-2 app is its own container and needs
``AW_APP_SELF_HOST``. A Tier-1 app IS the aw-workspace process, so
``socket.gethostname()`` returns exactly the value ``ContainerSupervisor``
injects into sibling containers as ``AW_WORKSPACE_HOST`` — no extra env var.
Tier-1 routes are IdentityGuard-gated, hence the ``X-Api-Key`` header on our own
entry (see ``docs/app-workspace-api-auth.md``).
"""

from __future__ import annotations

import json
import logging
import os
import socket

log = logging.getLogger("aw-app-marketing")

OWN_SERVER_NAME = "marketing"
META_SERVER_NAME = "meta-ads"

#: Meta's own hosted Ads MCP server, announced 2026-07-16. We register it; we do
#: not reimplement any of it. Campaign/ad-set/ad creation, reporting, catalogs
#: and A/B tests are all its tools, not ours.
META_MCP_URL = "https://mcp.facebook.com/ads"


def mcp_json_path(package_dir: str) -> str:
    return os.path.join(package_dir, "mcp.json")


def _own_entry(port: int) -> dict:
    entry: dict = {
        "type": "http",
        "url": f"http://{socket.gethostname()}:{port}/api/apps/marketing/mcp",
        "enabled": True,
    }
    api_key = os.environ.get("AW_WORKSPACE_API_KEY")
    if api_key:
        entry["headers"] = {"X-Api-Key": api_key}
    return entry


def _meta_entry(token: str) -> dict:
    """Meta's upstream — ``enabled`` only once a token actually exists.

    An upstream registered with an empty (or placeholder) Bearer does not fail
    loudly: it connects, gets 401, and serves zero tools. That reads as a broken
    app rather than an unconfigured one, which is the exact silent degradation
    ``aw-workspace-cli doctor`` exists to surface. So: no token → ``enabled:
    false`` and no ``Authorization`` header at all.
    """
    entry: dict = {
        "type": "http",
        "url": META_MCP_URL,
        "enabled": bool(token),
    }
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return entry


def build_document(port: int, config: dict | None) -> dict:
    """The complete ``mcp.json`` document — pure, so tests can assert its shape
    without touching the filesystem or a real token."""
    token = str((config or {}).get("meta_access_token") or "").strip()
    return {
        "mcpServers": {
            OWN_SERVER_NAME: _own_entry(port),
            META_SERVER_NAME: _meta_entry(token),
        }
    }


def register_self(package_dir: str, port: int, config: dict | None = None) -> bool:
    """Write both upstreams into ``mcp.json``. Returns True if the file changed.

    Best-effort by design: a bare dev run with no package dir simply no-ops, and
    an unwritable path is a warning, not a failed activation — an app with fewer
    tools beats an app whose routes and window are gone too.
    """
    if not os.path.isdir(package_dir):
        return False

    doc = build_document(port, config)
    path = mcp_json_path(package_dir)

    try:
        with open(path, encoding="utf-8") as fh:
            if json.load(fh) == doc:
                return False  # already correct — don't churn the gateway
    except (OSError, ValueError):
        pass

    try:
        tmp = f"{path}.tmp.{os.getpid()}"
        # 0600: this file holds the workspace API key and the Meta token.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("aw-app-marketing: could not write %s: %s", path, exc)
        return False

    meta_enabled = doc["mcpServers"][META_SERVER_NAME]["enabled"]
    log.info(
        "aw-app-marketing: registered upstreams %r (%s) and %r (enabled=%s)",
        OWN_SERVER_NAME, doc["mcpServers"][OWN_SERVER_NAME]["url"],
        META_SERVER_NAME, meta_enabled,
    )
    if not meta_enabled:
        log.warning(
            "aw-app-marketing: %r upstream is DISABLED — no meta_access_token in "
            "this app's config. Nothing can be created on Meta until one is saved "
            "via POST /api/apps/marketing/config.", META_SERVER_NAME,
        )
    return True
