"""Reading this app's per-install config, and saying clearly when it is empty.

Every value here (except ``meta_access_token``, see below) is a **per-
installation** setting that lives in the workspace's ``config_store``
(``<AW_WORKSPACE_HOME>/app-config/marketing.json``), written via
``POST /api/apps/marketing/config`` and read back through ``ctx.config``. None
of it is versioned in this repo — the repo carries only the schema
(``aw-app.json``'s ``config_schema``).

Two things it is deliberately NOT:

* **not ``aw-secrets``.** That vault is human-gated: every read interrupts a
  person on Telegram. The gateway needs the Meta token on every activation, with
  nobody in the loop, so a per-install value is the right surface.
* **not a default.** Nothing here invents a country, a budget or an account id.
  An unset value produces a refusal naming the missing field, because the
  alternative is an agent quietly spending money somewhere nobody chose.

``meta_access_token`` is the one exception to "lives in config": it is a ~60-day
bearer credential (``ads_mcp_management`` scope), so it is routed to the
workspace's encrypted secret store instead — ``ctx.secrets``, written via
``POST /api/apps/marketing/settings``, never through the generic config path
(``src/apps/routes.py``'s ``save_app_config`` has no concept of ``x-secret`` and
would otherwise land it in plain, cloud-synced config). Same pattern as
aw-app-notion's ``notion_token`` / aw-app-git's ``github_token`` /
aw-app-android-studio's ``remote_token``. Every function below that needs it
takes it as an explicit ``token`` argument rather than reading it out of
``config``, so a caller cannot accidentally source it from the wrong place.
"""

from __future__ import annotations

from typing import Any

#: Config (non-secret) fields that must be set before anything can be created
#: on Meta's side. ``meta_access_token`` is checked separately (see ``missing``)
#: because it lives in the secret store, not in ``config``.
#: ``meta_instagram_actor_id`` is deliberately absent — it is only required for
#: Instagram placements, and ``planner``/``creative`` ask for it there.
REQUIRED_CONFIG_FOR_META = ("meta_ad_account_id", "meta_page_id")


def get(config: dict[str, Any] | None, key: str, default: Any = "") -> Any:
    """Read a config value, treating whitespace-only strings as unset."""
    value = (config or {}).get(key, default)
    if isinstance(value, str):
        value = value.strip()
    return value if value not in ("", None) else default


def missing(config: dict[str, Any] | None, token: str | None,
            keys: tuple[str, ...] = REQUIRED_CONFIG_FOR_META) -> list[str]:
    absent = [k for k in keys if not get(config, k)]
    if not (token or "").strip():
        absent = ["meta_access_token", *absent]
    return absent


def readiness(config: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
    """A machine- and human-readable "is this app actually usable yet" answer.

    Surfaced on ``GET /api/apps/marketing/status`` and folded into every tool
    result that depends on a Meta-side value, so "it did nothing" always comes
    with the reason attached rather than looking like a bug.
    """
    absent = missing(config, token)
    return {
        "configured": not absent,
        "missing": absent,
        "meta_upstream_enabled": bool((token or "").strip()),
        "instagram_ready": bool(get(config, "meta_instagram_actor_id")),
        "defaults": {
            "daily_budget_minor": int(get(config, "default_daily_budget_minor", 0) or 0),
            "country": get(config, "default_country"),
            "currency": get(config, "default_currency"),
        },
        "detail": (
            "Configured."
            if not absent
            else (
                "Not configured: "
                + ", ".join(absent)
                + ". Set meta_access_token with POST /api/apps/marketing/settings "
                  "and the rest with POST /api/apps/marketing/config (or the app's "
                  "Settings gear). Until then the meta-ads upstream stays disabled "
                  "and no campaign can be created — the local planning tools still "
                  "work and are worth using to prepare the brief."
            )
        ),
    }
