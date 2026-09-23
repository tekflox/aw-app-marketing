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
changed gets a new row; the history is the point.

**Append-only and idempotent are about different things, and conflating them
lost data.** Idempotency means "this campaign already exists on Meta, do not
create it again" — a money-safety signal. Append-only means "a row is never
rewritten". The first version of this module used *row exists* as the answer to
both, so a second ``record()`` call carrying the ``ad_id`` and ``creative_id``
of an ad created after the campaign was a silent no-op, and the audit trail for
a real campaign kept null ids forever. Now a later call that says something new
**appends a revision row**, and reads **fold** every row for a campaign into
the current view of it. Both guarantees survive: nothing is rewritten, and
``already_recorded`` still means exactly what it meant.

**Two kinds of row, and they must never answer for each other.** A ``creation``
row (written by ``record()``) is what ``already_recorded`` and folding reason
about. An ``activation`` row (written by ``record_event()``) is a distinct
event — an approval outcome, or an activation attempt's result — and is never
folded into the creation view, never satisfies ``record()``'s idempotency
check, and never counts as a campaign of its own in ``list()``. See ``find``'s
``entry_type``.
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


#: ``recorded_at`` is when a row was written, not something the caller asserts
#: about the campaign — so it never counts as new information, and folding
#: keeps the FIRST one (when the campaign was launched). Every row's timestamp
#: stays visible in the folded ``history``. ``entry_type`` is metadata about
#: the row's kind, not campaign content — every row folded together here is
#: already the same kind (see ``find``/``record``/``list``), so treating it as
#: content would only add noise for a legacy row that predates the field.
FOLD_IGNORES = ("recorded_at", "entry_type")


def _says_something_new(current: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Which fields of ``candidate`` the folded ``current`` view does not already have.

    A **falsy** candidate field (``None``, ``""``, ``[]``) is "no opinion", never
    a difference. This is load-bearing rather than tidy: the second call of a
    two-step creation passes a trimmed plan, and treating its empty ``products``
    or absent ``brief`` as a change would blank what the first call recorded.
    """
    return sorted(
        key for key, value in candidate.items()
        if key not in FOLD_IGNORES and value and current.get(key) != value
    )


def _fold(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Merge every row recorded for one campaign into the current view of it,
    plus the history of how it got there.

    The oldest row is the base, so the folded entry keeps the full key set (a
    field nobody ever filled stays present and null, rather than vanishing).
    Each later row overwrites only where it says something new, by the same rule
    :func:`_says_something_new` uses. ``history`` carries one item per row — its
    timestamp and the fields it introduced — so both steps of a two-call
    creation stay recoverable from the tool result, not only from the JSONL.
    """
    merged = dict(rows[0])
    history = [{"recorded_at": rows[0].get("recorded_at"),
                "fields": _says_something_new({}, rows[0])}]
    for row in rows[1:]:
        introduced = _says_something_new(merged, row)
        merged.update({key: row[key] for key in introduced})
        history.append({"recorded_at": row.get("recorded_at"), "fields": introduced})
    return merged, history


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
        """The current view for *entry_type* on this campaign, or ``None``.

        Defaults to the creation row, because that is what ``record()`` calls
        this for: its idempotency check, "does this campaign already exist on
        Meta". An activation row must never answer that lookup — it carries no
        opinion on whether the campaign was already created, only on whether
        it was later switched on — so it is filtered out by type before
        anything else happens. Rows written before ``entry_type`` existed
        carry none, and are treated as ``creation`` — the only kind that
        existed then.

        For ``creation``, the matching rows are **folded**: not the first
        match, because a campaign created in two calls has more than one row,
        and the oldest is precisely the one missing the ids the second call
        brought. Activation rows are distinct events rather than revisions of
        one another, so the most recent one wins instead.
        """
        rows = [e for e in self.read_all()
                if e.get("campaign_id") == campaign_id
                and (e.get("entry_type") or ENTRY_TYPE_CREATION) == entry_type]
        if not rows:
            return None
        if entry_type == ENTRY_TYPE_CREATION:
            return _fold(rows)[0]
        return rows[-1]

    def _append(self, entry: dict[str, Any]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def record(self, plan: dict[str, Any] | None, meta_ids: dict[str, Any] | None,
               *, config: dict[str, Any] | None = None,
               notes: str | None = None) -> dict[str, Any]:
        """Record what Meta created. Never rewrites a row.

        Three outcomes, and ``already_recorded`` distinguishes only the first
        from the other two, because it answers one question — *is this campaign
        already on Meta?* — and that answer is what stops the flow re-creating
        it and spending real money twice:

        * unknown ``campaign_id`` → append, ``already_recorded: False``;
        * known, and this call says nothing new → **no-op**, return the folded
          view with ``already_recorded: True``;
        * known, and this call brings ids or values the ledger lacks (the
          ``ad_id``/``creative_id`` of an ad created after the campaign) →
          **append a revision row**, return the folded view with
          ``already_recorded: True`` plus ``revision`` and ``updated_fields``.
        """
        from . import config as cfg

        meta_ids = dict(meta_ids or {})
        campaign_id = str(meta_ids.get("campaign_id") or "").strip()
        if not campaign_id:
            raise ValueError(
                "meta_ids.campaign_id is required — record what Meta actually "
                "created, not what was planned."
            )

        existing_rows = [e for e in self.read_all()
                         if e.get("campaign_id") == campaign_id
                         and (e.get("entry_type") or ENTRY_TYPE_CREATION) == ENTRY_TYPE_CREATION]
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

        if existing_rows:
            folded, _ = _fold(existing_rows)
            updated = _says_something_new(folded, entry)
            if not updated:
                return {**folded, "already_recorded": True}
            self._append(entry)
            folded, history = _fold(existing_rows + [entry])
            log.info("aw-app-marketing: revised campaign %s (%s)",
                     campaign_id, ", ".join(updated))
            return {**folded, "already_recorded": True,
                    "revision": len(history), "updated_fields": updated}

        self._append(entry)
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
        """Campaigns, not rows: every revision of a campaign folds into one
        entry carrying its ``revision`` count and its ``history``. ``total`` is
        therefore distinct campaigns — anything counting lines to count
        campaigns is wrong now.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in self.read_all():
            if (entry.get("entry_type") or ENTRY_TYPE_CREATION) != ENTRY_TYPE_CREATION:
                continue
            grouped.setdefault(str(entry.get("campaign_id")), []).append(entry)
        campaigns = []
        for rows in grouped.values():
            folded, history = _fold(rows)
            campaigns.append({**folded, "revision": len(history), "history": history})
        recent = campaigns[-limit:][::-1] if limit > 0 else []
        return {"total": len(campaigns), "returned": len(recent), "campaigns": recent,
                "ledger_path": self.path}
