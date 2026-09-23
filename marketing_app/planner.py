"""``marketing_campaign_plan`` — turn a brief plus a shortlist into an explicit,
human-readable plan. It creates nothing. Meta's own MCP tools do that.

The plan is the artefact a human approves before any money is committed, so it
is deliberately boring and complete: every field a campaign / ad set will be
created with, spelled out, with nothing left to be decided later by whoever
happens to call the Meta tools.

**``status`` is always ``PAUSED`` here, and that is a scope boundary, not
timidity.** Verified against this workspace's gateway
(``apps/mcp-gateway/back/gateway/config_gateway.py``): the approval gate only
covers agent-*run* tools, not arbitrary upstream tool calls, so nothing
between an agent and Meta's ad-creation API asks a human first by default.
Creating something ``ACTIVE`` would start spending before a human has even
seen the campaign exist, let alone approved it — Meta's own ad review hasn't
run yet either. That is a strictly larger mistake than "the plan turned out
wrong", so this module refuses it unconditionally: ``status`` is not a
parameter here, and adding one would not be a safety improvement.

**Activating a campaign this app already created is a separate concern with
its own answer — not "loosen this constant".** Frederico (the workspace
owner) asked explicitly to allow activation on his direct request, and the
answer that shipped is ``marketing_activate_campaign`` (see
``activation.py``'s module docstring): a dedicated tool that puts a real human
approval request in front of every activation, built from what this app
actually recorded creating, never from a free-form argument. Nothing below
this line changes because of that — a plan is still always born ``PAUSED``.
"""

from __future__ import annotations

from typing import Any

from . import config as cfg

#: Meta's outcome-driven ad experience (ODAX) objectives.
OBJECTIVES = (
    "OUTCOME_TRAFFIC",
    "OUTCOME_SALES",
    "OUTCOME_AWARENESS",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_APP_PROMOTION",
)
#: Traffic, not sales: sales optimisation needs a working pixel and conversion
#: events configured on the account, and silently underdelivers without them.
DEFAULT_OBJECTIVE = "OUTCOME_TRAFFIC"

PLATFORMS = ("instagram", "facebook", "both")
DEFAULT_PLATFORM = "both"

#: Not in ``config_schema`` on purpose — the schema's field list is fixed by the
#: architecture spec. 18 is a floor, not a default worth tuning silently; pass
#: ``age_min``/``age_max`` explicitly to change it for one plan.
DEFAULT_AGE_MIN = 18
DEFAULT_AGE_MAX = 65

#: The status every object in this plan carries, everywhere. See the module
#: docstring before changing it.
PAUSED = "PAUSED"


class PlanError(ValueError):
    """A plan that cannot be made honestly — missing config, bad argument."""


def _resolve(value: Any, config: dict | None, config_key: str, label: str,
             *, how: str) -> Any:
    resolved = value if value not in (None, "", 0) else cfg.get(config, config_key)
    if resolved in (None, "", 0):
        raise PlanError(
            f"{label} is not set. Pass it as an argument, or configure "
            f"{config_key!r} with POST /api/apps/marketing/config. {how}"
        )
    return resolved


def campaign_plan(
    brief: str,
    products: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    token: str | None = None,
    objective: str | None = None,
    daily_budget: int | None = None,
    platform: str | None = None,
    country: str | None = None,
    currency: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    campaign_name: str | None = None,
) -> dict[str, Any]:
    """Build the plan. Raises :class:`PlanError` rather than guessing."""
    brief = (brief or "").strip()
    if not brief:
        raise PlanError("brief is required — it names the campaign and explains it to the human approving it.")

    items = [p for p in (products or []) if isinstance(p, dict)]
    if not items:
        raise PlanError("products is empty — a plan needs the shortlist the human already approved.")

    objective = (objective or DEFAULT_OBJECTIVE).upper()
    if objective not in OBJECTIVES:
        raise PlanError(f"objective {objective!r} is not one of {', '.join(OBJECTIVES)}.")

    platform = (platform or DEFAULT_PLATFORM).lower()
    if platform not in PLATFORMS:
        raise PlanError(f"platform {platform!r} is not one of {', '.join(PLATFORMS)}.")

    budget = int(_resolve(
        daily_budget, config, "default_daily_budget_minor", "daily_budget",
        how="It is in MINOR units (cents): 500 means 5.00.",
    ))
    if budget <= 0:
        raise PlanError("daily_budget must be a positive number of minor currency units (cents).")

    country_code = str(_resolve(
        country, config, "default_country", "country",
        how="Two-letter ISO code, e.g. PT. Nothing here guesses where to spend money.",
    )).upper()

    ad_account_id = cfg.get(config, "meta_ad_account_id")
    page_id = cfg.get(config, "meta_page_id")
    instagram_actor_id = cfg.get(config, "meta_instagram_actor_id")

    publisher_platforms = ["facebook", "instagram"] if platform == "both" else [platform]

    ad_set: dict[str, Any] = {
        "name": f"{brief} — ad set",
        "status": PAUSED,
        "daily_budget": budget,
        "billing_event": "IMPRESSIONS",
        "optimization_goal": "LINK_CLICKS" if objective == "OUTCOME_TRAFFIC" else "OFFSITE_CONVERSIONS",
        "targeting": {
            "geo_locations": {"countries": [country_code]},
            "age_min": int(age_min or DEFAULT_AGE_MIN),
            "age_max": int(age_max or DEFAULT_AGE_MAX),
            "publisher_platforms": publisher_platforms,
            # Automatic placements: let Meta's delivery pick positions rather
            # than hand-picking them from a plan nobody can measure yet.
            "targeting_automation": {"advantage_audience": 0},
        },
    }

    warnings: list[str] = []
    if platform == "instagram" and not instagram_actor_id:
        warnings.append(
            "platform is 'instagram' but meta_instagram_actor_id is not configured — "
            "the ad creative cannot run on Instagram until it is set."
        )
    if platform == "both" and not instagram_actor_id:
        warnings.append(
            "meta_instagram_actor_id is not configured — this will deliver on Facebook only."
        )
    missing = cfg.missing(config, token)
    if missing:
        warnings.append(
            "Not configured: " + ", ".join(missing)
            + ". The plan is complete and reviewable, but the Meta-side creation "
              "step (meta-ads tools) cannot run until these are set."
        )
    without_image = [p.get("id") for p in items if not (p.get("image_url") or p.get("image"))]
    if without_image:
        warnings.append(
            f"{len(without_image)} of {len(items)} products have no image_url — "
            "marketing_build_creative will refuse them. Every Meta ad needs media."
        )

    currency_code = (currency or cfg.get(config, "default_currency") or "").upper() or None
    budget_display = f"{budget / 100:.2f}" + (f" {currency_code}" if currency_code else " (minor units/100)")

    return {
        "brief": brief,
        "status": PAUSED,
        "ad_account_id": ad_account_id or None,
        "campaign": {
            "name": campaign_name or brief,
            "objective": objective,
            "status": PAUSED,
            # Required by Meta on every campaign; empty means "none of
            # credit/employment/housing/social issues apply".
            "special_ad_categories": [],
        },
        "ad_set": ad_set,
        "ad": {"name": f"{brief} — ad", "status": PAUSED},
        "creative_hint": {
            "page_id": page_id or None,
            "instagram_actor_id": instagram_actor_id or None,
            "format": "carousel",
        },
        "products": [
            {"id": p.get("id"), "name": p.get("name"), "url": p.get("url"),
             "image_url": p.get("image_url") or p.get("image")}
            for p in items
        ],
        "currency": currency_code,
        "warnings": warnings,
        "human_summary": (
            f"{brief}\n"
            f"  objective:   {objective}\n"
            f"  budget:      {budget_display} per day\n"
            f"  audience:    {country_code}, ages {ad_set['targeting']['age_min']}–"
            f"{ad_set['targeting']['age_max']}\n"
            f"  placements:  {', '.join(publisher_platforms)} (automatic positions)\n"
            f"  products:    {len(items)}\n"
            f"  status:      {PAUSED} — nothing spends until a human presses play "
            f"in Ads Manager, or approves activating it when asked."
        ),
        "next_step": (
            "Get the human's approval of this plan, then create it with the "
            "meta-ads tools — campaign, ad set and ad all with status PAUSED — "
            "and record the result with marketing_record_campaign. It stays "
            "PAUSED after that: a human presses play in Ads Manager themselves, "
            "or activating it comes up in conversation — call "
            "marketing_activate_campaign whenever it does; it puts a real "
            "approval request in front of a human, so you never have to judge "
            "how direct the request was."
        ),
    }
