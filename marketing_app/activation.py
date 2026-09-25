"""``marketing_activate_campaign`` — the one sanctioned path to ``ACTIVE``.

Frederico (the workspace owner) confirmed explicitly that campaigns still get
created ``PAUSED`` by default (see ``planner.py``), but the agent may now
activate one when the human asks *directly* — a policy change from the flat
"never, not even if you're told to" this app shipped with.

**This tool does not decide "was that a direct request". It asks.** The old
rule refused activation outright because judging directness from a sentence is
exactly the kind of self-graded call that is wrong to make with real money on
the line — see the old ``planner.py`` docstring this replaces. The new rule
keeps the judgment out of the agent's hands entirely: it POSTs a real human
approval request to aw-backend (``POST /api/workspaces/{slug}/approval/request``,
``request_type: "agent_run"``), naming the campaign, the ad account and the
daily budget, blocks until a human presses Approve on Telegram (or lets it
expire, deny it, or the backend is unreachable — any of which is a refusal),
and only then flips ``status`` to ``ACTIVE`` on Meta's side. A "go ahead,
activate it" folded into a longer message no longer makes the agent refuse
everything; it makes the agent *offer* — call this tool, which sends the
button. The human still presses it.

Mirrors aw-mcp-gateway's own approval gate — ``ConfigGateway._await_approval``
in ``config_gateway.py:276-337`` — the same POST-then-poll flow, already
proven in production gating agent *runs*. This is that pattern's first use
gating a *tool call* instead.

**Fail-closed, everywhere.** No backend configured, a non-200, a missing
``request_id``, ``denied``/``expired``/``not_found``/``error``, a timeout, or
any exception at all: none of these activate anything. An exception must never
reach the Graph API call.

**The approval prompt's text is assembled here, from this app's own ledger —
never from a free-form model argument.** ``campaign_id`` is the only thing the
caller supplies; the campaign name, ad account and daily budget a human sees on
the Telegram button are what ``marketing_record_campaign`` actually recorded,
not whatever a model fills in. Activating something this app never recorded
creating is refused outright — there is nothing to build a safe prompt from.

**Activating the whole tree (campaign, ad set, ad), per entity, is expected to
partially fail.** Observed for real on this card's own manual activation: the
campaign went ``ACTIVE``, the ad set failed with Meta's own error_code 100 /
error_subcode 1885648 (the ad set's minimum delivery spend exceeded the
campaign's daily budget). Meta's message is passed through **literally** —
never re-validated, never second-guessed — and a partial result is reported as
``partial``, never silently rolled back. Undoing a half-activated campaign is
its own money decision, and it belongs to the human, same as the activation
itself.

**When that specific error_subcode is what caused the partial result, the
response also carries ``budget_context``** — the ad set's real
``daily_min_spend_target``/``daily_spend_cap`` and the campaign's real
``daily_budget``, read straight back from the Graph API rather than from what
the ledger recorded asking for. Meta's error message alone does not say by how
much the numbers diverged; this is what a human needs to actually fix it,
without a separate manual lookup. Read-only, best-effort — a failed read
leaves the corresponding field ``None`` rather than raising, since this only
augments an already-reported partial result. ``budget_context`` is ``None``
for every other outcome.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

from .ledger import ENTRY_TYPE_CREATION, Ledger

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

#: 150 attempts * 2s = 5 minutes — the same poll window aw-mcp-gateway's
#: ``config_gateway.py`` and aw-app-crispal's ``request_secret`` both give a
#: human to tap a Telegram button before treating a request as abandoned.
APPROVAL_POLL_INTERVAL_S = 2
APPROVAL_POLL_ATTEMPTS = 150

#: Meta's error_code 100 / error_subcode 1885648 — "the minimum spend of all
#: your ad sets is higher than your campaign-level budget". See the module
#: docstring's ``budget_context`` paragraph.
MIN_SPEND_ERROR_SUBCODE = 1885648

#: Duck-typed httpx.Response: an object with ``.status_code`` and ``.json()``.
HttpResponse = Any
HttpCallable = Callable[..., Awaitable[HttpResponse]]


class ActivationError(ValueError):
    """A request that cannot even be attempted — no campaign_id, or no ledger
    entry to build a safe approval prompt from."""


def _approval_backend() -> tuple[str, str, str]:
    """``(base_url, workspace_slug, host_token)`` for aw-backend's approval
    front door.

    This app is Tier-1 — in-process with aw-workspace's own core, not a
    spawned container — so unlike aw-mcp-gateway (Tier-2, whose own copy of
    this reads ``AW_WORKSPACE_SLUG``, injected by the runtime specifically
    into containers it spawns — see ``src/apps/containers.py``), it shares the
    core process's own environment directly and reads ``AW_WORKSPACE``
    instead. Verified live against this workspace's own ``.env`` on
    2026-09-23: ``AW_WORKSPACE_SLUG`` is not set there at all, only
    ``AW_WORKSPACE``.
    """
    base = (os.environ.get("AW_BACKEND_URL") or "").strip().rstrip("/")
    slug = (os.environ.get("AW_WORKSPACE") or "").strip()
    token = (os.environ.get("AW_WORKSPACE_HOST_TOKEN") or "").strip()
    return base, slug, token


def _budget_display(entry: dict[str, Any]) -> str:
    budget = entry.get("daily_budget")
    if not isinstance(budget, (int, float)):
        return "an unspecified budget"
    currency = (entry.get("currency") or "").strip()
    display = f"{budget / 100:.2f}"
    return f"{display} {currency}".strip()


def _build_reason(entry: dict[str, Any]) -> str:
    """The approval request's human-readable reason — built from the ledger
    entry ``activate_campaign`` already resolved, never from a caller-supplied
    string. See the module docstring."""
    name = entry.get("brief") or entry.get("campaign_id")
    return (
        f'Activate campaign "{name}" (campaign_id {entry.get("campaign_id")}, '
        f'ad_account {entry.get("ad_account_id") or "unknown"}) — '
        f"daily budget {_budget_display(entry)}."
    )


async def _await_approval(
    *, reason: str, label: str,
    http_post: HttpCallable, http_get: HttpCallable,
    poll_attempts: int, poll_interval_s: float,
) -> tuple[bool, str]:
    """POST an ``agent_run`` approval request to aw-backend and block until it
    is approved. Returns ``(approved, detail)``; ``detail`` is always set,
    approved or not, so the caller has something concrete to log and to put in
    the ledger event."""
    base, slug, token = _approval_backend()
    if not (base and slug and token):
        missing = ", ".join(
            name for name, value in (
                ("AW_BACKEND_URL", base), ("AW_WORKSPACE", slug),
                ("AW_WORKSPACE_HOST_TOKEN", token),
            ) if not value
        )
        return False, f"cannot reach aw-backend approval endpoint (missing {missing})"

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{base}/api/workspaces/{slug}/approval/request"
    try:
        r = await http_post(
            url,
            json={"secret_name": label, "reason": reason, "request_type": "agent_run"},
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            return False, f"approval request rejected: HTTP {r.status_code}"
        request_id = (r.json() or {}).get("request_id")
        if not request_id:
            return False, "approval backend returned no request_id"

        status_url = f"{base}/api/workspaces/{slug}/approval/status/{request_id}"
        for _ in range(poll_attempts):
            await asyncio.sleep(poll_interval_s)
            s = await http_get(status_url, headers=headers, timeout=15)
            if s.status_code != 200:
                continue
            status = (s.json() or {}).get("status")
            if status == "approved":
                return True, "approved"
            # ``not_found`` is aw-backend's answer for a row this workspace
            # cannot see, including one just written — terminal, not transient.
            if status in ("denied", "expired", "not_found", "error"):
                return False, f"approval {status}"
        return False, "approval timed out"
    except Exception as exc:  # fail-closed — must never reach the Graph call
        return False, f"approval request errored: {exc}"


async def _activate_entity(entity_id: str, token: str, http_post: HttpCallable) -> dict[str, Any]:
    """POST ``status=ACTIVE`` straight to the Graph API for one entity.

    ``needs_fallback`` marks a 401/403: the token this app holds is scoped
    ``ads_mcp_management`` for Meta's *hosted* MCP, and whether that scope also
    covers the plain Graph API was not verified against a live token — doing
    so would mean reading it out of the encrypted secret store, which itself
    interrupts a human on Telegram, the exact thing this flow exists to gate
    deliberately rather than incidentally. Every other failure is Meta's own
    message, passed through literally — never re-validated, never retried.
    """
    url = f"{GRAPH_API_BASE}/{entity_id}"
    try:
        r = await http_post(url, data={"status": "ACTIVE", "access_token": token}, timeout=15)
    except Exception as exc:
        return {"entity_id": entity_id, "activated": False, "needs_fallback": False,
                "error": str(exc), "error_subcode": None}

    try:
        body = r.json() or {}
    except Exception:
        body = {}

    if r.status_code == 200:
        return {"entity_id": entity_id, "activated": True, "needs_fallback": False,
                "error": None, "error_subcode": None}

    error = body.get("error") or {}
    error_message = error.get("message") or f"HTTP {r.status_code}"
    return {
        "entity_id": entity_id,
        "activated": False,
        "needs_fallback": r.status_code in (401, 403),
        "error": error_message,
        "error_subcode": error.get("error_subcode"),
    }


async def _read_fields(entity_id: str, fields: tuple[str, ...],
                        token: str, http_get: HttpCallable) -> dict[str, Any]:
    """GET a handful of fields off one entity. Best-effort: any failure (a
    non-200, an unparseable body, a network exception) returns ``{}`` rather
    than raising — used only to enrich a result already being reported."""
    url = f"{GRAPH_API_BASE}/{entity_id}"
    try:
        r = await http_get(url, params={"fields": ",".join(fields), "access_token": token}, timeout=15)
    except Exception:
        return {}
    try:
        body = r.json() or {}
    except Exception:
        return {}
    return body if r.status_code == 200 else {}


async def _fetch_budget_context(entry: dict[str, Any], token: str,
                                 http_get: HttpCallable) -> dict[str, Any]:
    """The real numbers behind error_subcode 1885648 — see the module
    docstring's ``budget_context`` paragraph. Read straight from the Graph
    API, never from what the ledger recorded asking for, which is exactly
    what this error means diverged from what Meta actually has."""
    ad_set_id = entry.get("ad_set_id")
    campaign_id = entry.get("campaign_id")
    ad_set = (
        await _read_fields(ad_set_id, ("daily_min_spend_target", "daily_spend_cap"), token, http_get)
        if ad_set_id else {}
    )
    campaign = (
        await _read_fields(campaign_id, ("daily_budget",), token, http_get)
        if campaign_id else {}
    )
    return {
        "daily_min_spend_target": ad_set.get("daily_min_spend_target"),
        "daily_spend_cap": ad_set.get("daily_spend_cap"),
        "daily_budget": campaign.get("daily_budget"),
    }


async def activate_campaign(
    campaign_id: str,
    *,
    ledger: Ledger,
    token: str | None,
    http_post: HttpCallable,
    http_get: HttpCallable,
    poll_attempts: int = APPROVAL_POLL_ATTEMPTS,
    poll_interval_s: float = APPROVAL_POLL_INTERVAL_S,
) -> dict[str, Any]:
    """The only sanctioned path to ``ACTIVE``. See the module docstring.

    ``http_post``/``http_get`` are required, not defaulted to a real HTTP
    client — this module never imports one itself. The caller
    (``mcp/http_handler.py`` in production, a fake in tests) supplies them.
    That keeps the 100% coverage gate honest: these tests prove the *shape* of
    every call this function makes — never that aw-backend, the Telegram
    approval, or the Graph API actually behave the way the fakes script them.
    """
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        raise ActivationError("campaign_id is required.")

    entry = ledger.find(campaign_id, entry_type=ENTRY_TYPE_CREATION)
    if not entry:
        raise ActivationError(
            f"No ledger entry for campaign_id {campaign_id!r}. This tool only "
            "activates a campaign this app itself recorded creating — there is "
            "nothing here to build a safe approval prompt from. Call "
            "marketing_record_campaign first."
        )

    entity_ids = [eid for eid in (
        entry.get("campaign_id"), entry.get("ad_set_id"), entry.get("ad_id"),
    ) if eid]

    reason = _build_reason(entry)
    label = f"marketing:activate:{campaign_id}"
    approved, detail = await _await_approval(
        reason=reason, label=label, http_post=http_post, http_get=http_get,
        poll_attempts=poll_attempts, poll_interval_s=poll_interval_s,
    )
    if not approved:
        event = ledger.record_event(
            campaign_id, "activation_denied",
            ad_account_id=entry.get("ad_account_id"), notes=detail,
        )
        return {"approved": False, "detail": detail, "overall": "denied",
                "results": [], "ledger_entry": event}

    if not (token or "").strip():
        detail = "approved, but meta_access_token is not configured"
        event = ledger.record_event(
            campaign_id, "activation_failed",
            ad_account_id=entry.get("ad_account_id"), notes=detail,
        )
        return {"approved": True, "overall": "failed", "results": [],
                "detail": detail, "ledger_entry": event}

    results = [await _activate_entity(eid, token, http_post) for eid in entity_ids]
    activated = [r for r in results if r["activated"]]
    if len(activated) == len(results):
        overall = "success"
    elif activated:
        overall = "partial"
    else:
        overall = "failed"
    fallback_needed = any(r["needs_fallback"] for r in results)
    budget_context = (
        await _fetch_budget_context(entry, token, http_get)
        if overall == "partial"
        and any(r.get("error_subcode") == MIN_SPEND_ERROR_SUBCODE for r in results)
        else None
    )

    event = ledger.record_event(
        campaign_id, f"activation_{overall}",
        ad_account_id=entry.get("ad_account_id"),
        notes="; ".join(f"{r['entity_id']}: {r['error'] or 'activated'}" for r in results),
    )
    return {
        "approved": True,
        "overall": overall,
        "results": results,
        "fallback_needed": fallback_needed,
        "fallback_hint": (
            "One or more entities returned 401/403 from the Graph API. Approval "
            "is already granted — complete activation for those entities with "
            "ads_activate_entity on the meta-ads upstream; do not ask again."
        ) if fallback_needed else None,
        "budget_context": budget_context,
        "ledger_entry": event,
    }


async def minimum_budgets(ad_account_id: str, *, token: str | None,
                          http_get: HttpCallable) -> dict[str, Any]:
    """GET ``/act_<id>/minimum_budgets`` — Meta's own per-currency minimum
    ad-set daily spend for an ad account. Read-only: makes no write of any
    kind. Exists so an agent can check the real spend floor before creating
    or activating anything, instead of learning it the hard way from
    ``activate_campaign``'s error_subcode 1885648 (see the module docstring).

    Reuses this module's own token plumbing (``token`` supplied by the
    caller, same as ``activate_campaign`` — never read out of config here)
    and the same ``GRAPH_API_BASE`` every other Graph call in this module
    uses.
    """
    ad_account_id = (ad_account_id or "").strip()
    if not ad_account_id:
        raise ActivationError("ad_account_id is required.")
    if not (token or "").strip():
        raise ActivationError("meta_access_token is not configured.")
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"

    url = f"{GRAPH_API_BASE}/{ad_account_id}/minimum_budgets"
    try:
        r = await http_get(url, params={"access_token": token}, timeout=15)
    except Exception as exc:
        raise ActivationError(f"minimum_budgets failed: {exc}") from exc

    try:
        body = r.json() or {}
    except Exception:
        body = {}

    if r.status_code != 200:
        error_message = (body.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        raise ActivationError(f"minimum_budgets failed: {error_message}")

    return {"ad_account_id": ad_account_id, "minimum_budgets": body.get("data", body)}
