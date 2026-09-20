"""``marketing_build_creative`` — assemble a carousel ``object_story_spec`` and
validate it against Meta's hard limits before anything is sent anywhere.

Pure function, no network. That is the point: a carousel that violates a Meta
limit fails as an opaque Graph API error at creation time, minutes later, with
the agent having no idea which of ten products was the problem. Here the same
rule is a named refusal with the offending product ids in it, checked offline
and covered by tests.

Two refusals it will not talk you out of:

**Missing media.** Every Meta ad needs media — this is not a carousel-only
nicety. A product with no ``image_url`` cannot be a card, and guessing one
(scraping the product page, deriving a CDN path) is how you ship an ad with the
wrong shoe in it. Verified 2026-09-19: the store's MCP catalog surface exposes
``id, name, sku, brand, category, description, price, formatted_price, url`` and
no image field at all, so this refusal is currently the expected outcome and the
fix belongs in the store's WordPress plugin, not here.

**Promotional copy lifted from ``description``.** Real catalog descriptions
carry expired campaigns and coupon codes verbatim — ``"PROMOÇÃO Válida de
09/06/2023 a 30/10/2025"``, ``"Use o cupão MIMINHOCRISPAL"``. Copying those into
an ad publishes an expired offer, which is an ads-policy problem and not merely
a quality one. So description text is **extracted** — promo/coupon/date segments
dropped, first clean fragment kept — never copied.
"""

from __future__ import annotations

import re
from typing import Any

#: Meta's carousel bounds: at least 2 cards, at most 10.
MIN_CARDS = 2
MAX_CARDS = 10

#: Meta's recommended lengths. Longer is not rejected by the API but is
#: truncated in most placements, which reads as a broken ad.
MAX_HEADLINE = 40
MAX_CARD_DESCRIPTION = 30
MAX_PRIMARY_TEXT = 125

CALL_TO_ACTIONS = (
    "SHOP_NOW", "LEARN_MORE", "SIGN_UP", "BOOK_TRAVEL", "ORDER_NOW",
    "GET_OFFER", "SEE_MORE", "BUY_NOW", "CONTACT_US", "SUBSCRIBE",
)
DEFAULT_CTA = "SHOP_NOW"

#: Anything matching these is a time-bound or conditional offer, and none of it
#: may reach an ad. Matched after accent-folding is NOT applied, so both
#: accented and unaccented spellings are listed where they differ.
_PROMO_PATTERNS = [
    r"\bpromo\w*\b",
    r"\bdescont\w*\b",
    r"\bsald\w*\b",
    r"\bcup[ãa]o\b", r"\bcupom\b", r"\bcoupon\b",
    r"\bc[óo]digo\b", r"\bcode\b",
    r"\bv[áa]lid[ao]\b",
    r"\bat[ée]\s+\d", r"\bde\s+\d{1,2}/\d{1,2}", r"\d{1,2}/\d{1,2}/\d{2,4}",
    r"\b\d{1,3}\s*%\b",
    r"\boferta\b", r"\bgr[áa]tis\b", r"\bfree shipping\b",
    r"\bsale\b", r"\boff\b",
    r"\benvio\s+gratuito\b",
]
_PROMO_RE = re.compile("|".join(_PROMO_PATTERNS), re.IGNORECASE)

#: Split on sentence enders AND on the bullet/emoji separators these
#: descriptions are actually written with, so one bad clause doesn't poison the
#: whole paragraph.
_SEGMENT_RE = re.compile(r"[.!?\n\r•|;]+|[✀-➿\U0001f300-\U0001faff]+")
_WHITESPACE_RE = re.compile(r"\s+")


class CreativeError(ValueError):
    """A creative that cannot be built without guessing."""


def clean_copy(text: Any, *, max_length: int) -> str:
    """Extract the first promo-free fragment of ``text``, or "" if there is none.

    Never invents, never paraphrases, never truncates mid-word.
    """
    if not text:
        return ""
    for segment in _SEGMENT_RE.split(str(text)):
        candidate = _WHITESPACE_RE.sub(" ", segment).strip(" -–—:,")
        if len(candidate) < 3 or _PROMO_RE.search(candidate):
            continue
        if len(candidate) <= max_length:
            return candidate
        # Truncate on a word boundary rather than mid-word; drop the fragment
        # entirely if even the first word does not fit.
        cut = candidate[:max_length].rsplit(" ", 1)[0].strip(" -–—:,")
        if cut:
            return cut
    return ""


def _card(product: dict[str, Any], cta: str) -> dict[str, Any]:
    link = str(product.get("url") or "").strip()
    image_url = str(product.get("image_url") or product.get("image") or "").strip()
    name = _WHITESPACE_RE.sub(" ", str(product.get("name") or "")).strip()
    headline = name if len(name) <= MAX_HEADLINE else name[:MAX_HEADLINE].rsplit(" ", 1)[0]
    card: dict[str, Any] = {
        "link": link,
        "name": headline,
        "image_url": image_url,
        "call_to_action": {"type": cta, "value": {"link": link}},
    }
    description = clean_copy(product.get("description"), max_length=MAX_CARD_DESCRIPTION)
    if description:
        card["description"] = description
    return card


def build_creative(
    products: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    headline: str | None = None,
    primary_text: str | None = None,
    cta: str | None = None,
    platform: str = "both",
) -> dict[str, Any]:
    """Build the ``object_story_spec``. Raises :class:`CreativeError` on any
    limit violation, naming exactly which products are at fault."""
    from . import config as cfg

    items = [p for p in (products or []) if isinstance(p, dict)]
    cta = (cta or DEFAULT_CTA).upper()
    if cta not in CALL_TO_ACTIONS:
        raise CreativeError(f"cta {cta!r} is not one of {', '.join(CALL_TO_ACTIONS)}.")

    if len(items) < MIN_CARDS:
        raise CreativeError(
            f"a carousel needs at least {MIN_CARDS} products, got {len(items)}."
        )
    if len(items) > MAX_CARDS:
        raise CreativeError(
            f"a carousel takes at most {MAX_CARDS} products, got {len(items)}. "
            "Pick the top ones from the approved shortlist rather than dropping "
            "some silently."
        )

    page_id = cfg.get(config, "meta_page_id")
    if not page_id:
        raise CreativeError(
            "meta_page_id is not configured — Meta requires a Page on every ad "
            "creative. Set it with POST /api/apps/marketing/config."
        )

    missing_image = [p.get("id") for p in items if not (p.get("image_url") or p.get("image"))]
    missing_link = [p.get("id") for p in items if not p.get("url")]
    missing_name = [p.get("id") for p in items if not p.get("name")]
    if missing_image:
        raise CreativeError(
            f"no image_url on product(s) {missing_image}. Every Meta ad needs "
            "media and this app will not guess a URL. If the catalog itself "
            "exposes no image field, that is a fix in the store's own MCP "
            "plugin, not here — say so instead of working around it."
        )
    if missing_link:
        raise CreativeError(f"no url on product(s) {missing_link} — a carousel card must link somewhere.")
    if missing_name:
        raise CreativeError(f"no name on product(s) {missing_name} — a card needs a headline.")

    cards = [_card(p, cta) for p in items]
    message = clean_copy(primary_text, max_length=MAX_PRIMARY_TEXT) if primary_text else ""
    if primary_text and not message:
        raise CreativeError(
            "primary_text reads as promotional/time-bound copy (a coupon, a date "
            "range, a percentage off) and was rejected rather than published. "
            "Write evergreen copy, or leave it out."
        )

    link_data: dict[str, Any] = {
        "link": cards[0]["link"],
        "child_attachments": cards,
        "multi_share_optimized": True,
        "multi_share_end_card": False,
        "call_to_action": {"type": cta, "value": {"link": cards[0]["link"]}},
    }
    if message:
        link_data["message"] = message
    if headline:
        clean_headline = clean_copy(headline, max_length=MAX_HEADLINE)
        if not clean_headline:
            raise CreativeError(
                "headline reads as promotional/time-bound copy and was rejected. "
                "Write an evergreen headline, or leave it out."
            )
        link_data["name"] = clean_headline

    story: dict[str, Any] = {"page_id": page_id, "link_data": link_data}
    instagram_actor_id = cfg.get(config, "meta_instagram_actor_id")
    if instagram_actor_id and platform in ("instagram", "both"):
        story["instagram_actor_id"] = instagram_actor_id

    return {
        "object_story_spec": story,
        "cards": len(cards),
        "call_to_action": cta,
        "platform": platform,
        "dropped_copy": [
            p.get("id") for p in items
            if p.get("description") and not clean_copy(
                p.get("description"), max_length=MAX_CARD_DESCRIPTION)
        ],
        "notes": [
            "Card descriptions are EXTRACTED from the catalog description with "
            "promotional and date-bound segments removed — never copied whole.",
            "Pass this object_story_spec to the meta-ads ad-creative tool, then "
            "create the ad with status PAUSED.",
        ],
    }
