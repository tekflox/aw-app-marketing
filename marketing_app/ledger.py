"""``marketing_record_campaign`` / ``marketing_list_campaigns`` — the audit trail.

Append-only JSONL under the app's own durable data dir. Without it there is no
answer to "what did the agent launch last week", and no idempotency: an agent
that loses its place mid-flow has no way to tell whether it already created the
campaign it is about to create again, and re-running costs real money.

**Where it lives and why.** ``<AW_WORKSPACE_HOME>/data/marketing/campaigns.jsonl``
— the durable per-app tree that ``fs:workspace-data`` names. Package-relative
state looks persistent and is deleted wholesale on every app update/uninstall,
which for an audit log means it silently empties exactly when someone needs it.

**Append-only.** Nothing here updates or deletes a row. A campaign whose status
changed gets a new row via ``record_event`` — the history is the point.
``record`` (the creation row) stays idempotent on ``campaign_id`` and refuses
to duplicate; see ``find``'s ``entry_type`` for how the two kinds of row are
kept from answering for each other.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("aw-app-marketing")

APP_ID = "marketing"
LEDGER_NAME = "campaigns.jsonl"
DEFAULT_WORKSPACE_CONTAINER_DIR = "/opt/aw-workspace"

#: Form of the Ads Manager deep link. Confirmed by secondary sources, not by
#: Meta's own documentation — so if a meta-ads creation tool returns a permalink
#: of its own, that one wins (pass it as ``ads_manager_url``). Meta has moved
#: this host before (``business.facebook.com`` vs ``adsmanager.facebook.com``).
ADS_MANAGER_URL = (
    "https://business.facebook.com/adsmanager/manage/campaigns"
    "?act={ad_account_id}&selected_campaign_ids={campaign_id}"
)

#: Rows written by ``record()`` — one per campaign, the creation idempotency
#: key. Rows written before this field existed carry none and are treated as
#: this kind (see ``Ledger.find``).
ENTRY_TYPE_CREATION = "creation"
#: Rows written by ``record_event()`` — an activation attempt's outcome. Never
#: satisfies a ``record()`` idempotency lookup; see ``Ledger.find``.
ENTRY_TYPE_ACTIVATION = "activation"


def default_data_dir() -> str:
    """``<AW_WORKSPACE_HOME>/data/marketing`` — the layout the runtime binds for
    ``fs:workspace-data``.

    The fallback resolves against ``AW_WORKSPACE_CONTAINER_DIR`` rather than
    ``~``: the home dir differs between the workspace process and a spawned
    agent-runner sharing the same mount, and only the container dir is stable
    across both.
    """
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR),
        ".aw-workspace",
    )
    return os.path.join(home, "data", APP_ID)


def ads_manager_url(ad_account_id: str | None, campaign_id: str | None) -> str | None:
    if not ad_account_id or not campaign_id:
        return None
    account = str(ad_account_id)
    # Meta writes ad account ids as ``act_<digits>``; accept either spelling.
    if not account.startswith("act_"):
        account = f"act_{account}"
    return ADS_MANAGER_URL.format(ad_account_id=account, campaign_id=campaign_id)


class Ledger:
    def __init__(self, data_dir: str | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.path = os.path.join(self.data_dir, LEDGER_NAME)

    def read_all(self) -> list[dict[str, Any]]:
        """Every entry, oldest first. A corrupted line is skipped, not fatal —
        an unreadable row must not make the whole audit log unreadable."""
        entries: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        log.warning("aw-app-marketing: skipping unparseable ledger line %d", line_no)
        except FileNotFoundError:
            return []
        return entries

    def find(self, campaign_id: str, *, entry_type: str = ENTRY_TYPE_CREATION) -> dict[str, Any] | None:
        """The row satisfying *entry_type* for this campaign, or ``None``.

        Defaults to the creation row, because that is what ``record()`` calls
        this for: its idempotency check, "does this campaign already exist on
        Meta". An activation row must never answer that lookup — it carries no
        opinion on whether the campaign was already created, only on whether
        it was later switched on — so it is filtered out here rather than left
        for the caller to notice. Rows written before ``entry_type`` existed
        carry none, and are treated as ``creation`` — the only kind that
        existed then.
        """
        for entry in self.read_all():
            if entry.get("campaign_id") != campaign_id:
                continue
            if (entry.get("entry_type") or ENTRY_TYPE_CREATION) == entry_type:
                return entry
        return None

    def record(self, plan: dict[str, Any] | None, meta_ids: dict[str, Any] | None,
               *, config: dict[str, Any] | None = None,
               notes: str | None = None) -> dict[str, Any]:
        """Append one entry. Re-recording a known ``campaign_id`` is a no-op that
        returns the existing row — that is the idempotency the flow depends on."""
        from . import config as cfg

        meta_ids = dict(meta_ids or {})
        campaign_id = str(meta_ids.get("campaign_id") or "").strip()
        if not campaign_id:
            raise ValueError(
                "meta_ids.campaign_id is required — record what Meta actually "
                "created, not what was planned."
            )

        existing = self.find(campaign_id)
        if existing:
            return {**existing, "already_recorded": True}

        plan = dict(plan or {})
        account = (meta_ids.get("ad_account_id") or plan.get("ad_account_id")
                   or cfg.get(config, "meta_ad_account_id") or None)
        entry = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "entry_type": ENTRY_TYPE_CREATION,
            "campaign_id": campaign_id,
            "ad_set_id": meta_ids.get("ad_set_id") or meta_ids.get("adset_id"),
            "ad_id": meta_ids.get("ad_id"),
            "creative_id": meta_ids.get("creative_id"),
            "ad_account_id": account,
            "brief": plan.get("brief"),
            "objective": (plan.get("campaign") or {}).get("objective"),
            "daily_budget": (plan.get("ad_set") or {}).get("daily_budget"),
            "currency": plan.get("currency"),
            "status": plan.get("status") or "PAUSED",
            "products": [p.get("id") for p in (plan.get("products") or [])],
            # Meta's own permalink wins when the creation tool returned one —
            # it survives Meta moving the Ads Manager host.
            "ads_manager_url": (meta_ids.get("ads_manager_url")
                                or meta_ids.get("permalink")
                                or ads_manager_url(account, campaign_id)),
            "notes": notes,
        }

        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("aw-app-marketing: recorded campaign %s", campaign_id)
        return {**entry, "already_recorded": False}

    def record_event(self, campaign_id: str, event: str, *,
                      ad_account_id: str | None = None,
                      notes: str | None = None) -> dict[str, Any]:
        """Append an ACTIVATION-flow event row: an approval outcome, or the
        per-entity result of an activation attempt.

        Always appends — unlike ``record()`` there is no idempotency check
        here. A retried activation after a denial, a timeout, or a partial
        Meta-side failure is a distinct, real event worth its own row, not a
        duplicate of the first; ``activation.py`` is what decides whether to
        retry at all.
        """
        campaign_id = str(campaign_id or "").strip()
        if not campaign_id:
            raise ValueError("campaign_id is required.")
        entry = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "entry_type": ENTRY_TYPE_ACTIVATION,
            "campaign_id": campaign_id,
            "event": event,
            "ad_account_id": ad_account_id,
            "notes": notes,
        }
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("aw-app-marketing: recorded activation event %s for campaign %s",
                 event, campaign_id)
        return entry

    def list(self, limit: int = 20) -> dict[str, Any]:
        entries = self.read_all()
        recent = entries[-limit:][::-1] if limit > 0 else []
        return {"total": len(entries), "returned": len(recent), "campaigns": recent,
                "ledger_path": self.path}
