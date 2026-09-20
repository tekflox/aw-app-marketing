"""MCP server for Marketing, over Streamable HTTP (``POST /mcp``).

Same shape as ``aw-app-whiteboard``'s handler: aw-mcp-gateway runs in a sibling
container and cannot spawn a process inside aw-workspace, so a Tier-1 app's tool
surface is exposed as JSON-RPC 2.0 over HTTP — the wire protocol the gateway's
own ``HttpUpstream`` already speaks. ``self_register.py`` is what makes the
gateway find this endpoint.

**Every tool here is deterministic and makes no network calls.** Two things this
server deliberately cannot do:

* **create a campaign** — that is Meta's hosted MCP (``meta-ads`` upstream),
  registered alongside this one;
* **read a catalog** — that is the store's own app and its own credential, held
  by the agent, never by this app.

The agent is where the two meet. That is the whole architecture: the store
credential and the Meta credential never appear in the same process.
"""

from __future__ import annotations

import json
from typing import Any

from ..creative import CreativeError, build_creative
from ..filtering import filter_products
from ..ledger import Ledger
from ..planner import PlanError, campaign_plan

_PRODUCT_ARRAY = {
    "type": "array",
    "description": (
        "Products as returned by the STORE's own catalog tool — this app never "
        "fetches them. Each item: {id, name, sku, url, description, category, "
        "price, image_url}. Extra keys are ignored."
    ),
    "items": {"type": "object"},
}

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "marketing_filter_products",
        "description": (
            "Score a list of products against a collection hint ('coleção outono "
            "inverno', 'PV 24', '25/26') and return a ranked shortlist with the "
            "matched terms behind each score. Deterministic and accent/case "
            "insensitive: same catalog + same hint always gives the same "
            "shortlist, which is what makes the human approval step meaningful. "
            "YOU fetch the products first with the store's paginated catalog "
            "tool — this app has no access to any catalog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "products": _PRODUCT_ARRAY,
                "hint": {"type": "string", "description": "The collection/season the human asked for, in their own words."},
                "limit": {"type": "integer", "description": "Max products in the shortlist. Default 10."},
                "min_score": {"type": "number", "description": "Score floor, 0..1. Default 0.2."},
            },
            "required": ["products", "hint"],
        },
    },
    {
        "name": "marketing_campaign_plan",
        "description": (
            "Turn an approved shortlist into an explicit campaign plan — "
            "objective, daily budget, targeting, placements — for a human to "
            "approve. Creates nothing. Everything in the plan carries status "
            "PAUSED. Refuses rather than guessing when budget or country are "
            "neither passed nor configured."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "brief": {"type": "string", "description": "What this campaign is, in one line. Becomes the campaign name."},
                "products": _PRODUCT_ARRAY,
                "objective": {"type": "string", "description": "Meta ODAX objective. Default OUTCOME_TRAFFIC."},
                "daily_budget": {"type": "integer", "description": "Daily budget in MINOR units (cents). Falls back to default_daily_budget_minor."},
                "platform": {"type": "string", "description": "instagram | facebook | both. Default both."},
                "country": {"type": "string", "description": "Two-letter ISO country. Falls back to default_country."},
                "currency": {"type": "string", "description": "ISO currency for the human-readable summary. Falls back to default_currency."},
                "age_min": {"type": "integer", "description": "Minimum age. Default 18."},
                "age_max": {"type": "integer", "description": "Maximum age. Default 65."},
                "campaign_name": {"type": "string", "description": "Override the campaign name (defaults to the brief)."},
            },
            "required": ["brief", "products"],
        },
    },
    {
        "name": "marketing_build_creative",
        "description": (
            "Build a validated carousel object_story_spec from products, ready to "
            "hand to the meta-ads ad-creative tool. Enforces Meta's limits "
            "offline (2–10 cards, media + link + headline on every card) and "
            "REFUSES rather than guessing when a product has no image_url. Card "
            "copy is extracted from the catalog description with promotional, "
            "coupon and date-bound segments stripped — never copied whole, "
            "because catalog descriptions carry expired offers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "products": _PRODUCT_ARRAY,
                "headline": {"type": "string", "description": "Optional overall headline. Rejected if it reads as promotional."},
                "primary_text": {"type": "string", "description": "Optional body copy. Rejected if it reads as promotional."},
                "cta": {"type": "string", "description": "Meta call-to-action type. Default SHOP_NOW."},
                "platform": {"type": "string", "description": "instagram | facebook | both. Default both — decides whether instagram_actor_id is attached."},
            },
            "required": ["products"],
        },
    },
    {
        "name": "marketing_record_campaign",
        "description": (
            "Append what Meta actually created to this app's audit ledger, and "
            "return the Ads Manager link to hand to the human. Idempotent on "
            "campaign_id: re-recording the same campaign returns the existing "
            "entry instead of duplicating it. Call this immediately after the "
            "meta-ads tools succeed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan": {"type": "object", "description": "The plan object returned by marketing_campaign_plan."},
                "meta_ids": {
                    "type": "object",
                    "description": (
                        "What Meta returned: {campaign_id (required), ad_set_id, "
                        "ad_id, creative_id, ad_account_id, permalink}. Pass "
                        "Meta's own permalink when a tool returned one — it wins "
                        "over the link this app derives."
                    ),
                },
                "notes": {"type": "string", "description": "Free-text note stored with the entry."},
            },
            "required": ["meta_ids"],
        },
    },
    {
        "name": "marketing_list_campaigns",
        "description": (
            "List campaigns recorded by this app, most recent first — what was "
            "launched, when, for which products, with the Ads Manager link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many to return. Default 20."},
            },
        },
    },
]


def _ok(req_id: Any, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def _err(req_id: Any, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


async def handle_request(request: dict, *, config: dict[str, Any] | None = None,
                         ledger: Ledger | None = None) -> dict | None:
    """One JSON-RPC message in, one response out (or ``None`` for a notification)."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aw-app-marketing", "version": "1.0.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    name = request.get("params", {}).get("name", "")
    args = request.get("params", {}).get("arguments", {}) or {}
    ledger = ledger or Ledger()

    if name == "marketing_filter_products":
        result = filter_products(
            args.get("products") or [], args.get("hint") or "",
            limit=int(args.get("limit") or 10),
            min_score=float(args.get("min_score") if args.get("min_score") is not None else 0.2),
        )
        return _ok(req_id, _json(result))

    if name == "marketing_campaign_plan":
        try:
            plan = campaign_plan(
                args.get("brief") or "", args.get("products") or [], config=config,
                objective=args.get("objective"), daily_budget=args.get("daily_budget"),
                platform=args.get("platform"), country=args.get("country"),
                currency=args.get("currency"), age_min=args.get("age_min"),
                age_max=args.get("age_max"), campaign_name=args.get("campaign_name"),
            )
        except PlanError as exc:
            return _err(req_id, str(exc))
        return _ok(req_id, _json(plan))

    if name == "marketing_build_creative":
        try:
            creative = build_creative(
                args.get("products") or [], config=config,
                headline=args.get("headline"), primary_text=args.get("primary_text"),
                cta=args.get("cta"), platform=(args.get("platform") or "both").lower(),
            )
        except CreativeError as exc:
            return _err(req_id, str(exc))
        return _ok(req_id, _json(creative))

    if name == "marketing_record_campaign":
        try:
            entry = ledger.record(args.get("plan"), args.get("meta_ids"),
                                  config=config, notes=args.get("notes"))
        except (ValueError, OSError) as exc:
            return _err(req_id, str(exc))
        link = entry.get("ads_manager_url")
        tail = (
            f"\n\nSend this link to the human and STOP:\n{link}\n"
            "Activating the campaign, changing its budget or editing its "
            "targeting is done by a person in Ads Manager — never by this agent."
            if link else
            "\n\nNo Ads Manager link could be built (no ad_account_id). Record one "
            "with meta_ids.ad_account_id, or pass Meta's own permalink."
        )
        return _ok(req_id, _json(entry) + tail)

    if name == "marketing_list_campaigns":
        return _ok(req_id, _json(ledger.list(limit=int(args.get("limit") or 20))))

    return _err(req_id, f"Unknown tool: {name}")
