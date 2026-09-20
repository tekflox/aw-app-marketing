"""``marketing_filter_products`` — score a catalog against a collection hint.

Pure functions, no network, no catalog access. The **agent** fetches the
products (it holds the store credential in its tool belt); this app only scores
what it is handed. That boundary is the point: the store credential never enters
this app, and this app stays generic — it has never heard of any particular shop.

Why a tool at all, instead of letting the model eyeball the list? Because the
answer to "which products are in the autumn-winter collection" has to be
reproducible and reviewable. A model re-deciding it on every run produces a
different shortlist each time with no way to see why, and the human approving
the shortlist (which is mandatory — see the skill) has nothing to check against.

What the real catalog looks like, verified 2026-09-19 against an actual store —
this is why the matching is shaped the way it is:

* **The collection lives in free-text ``description``, not in the taxonomy.**
  A product reads ``"🌟 NOVA COLEÇÃO OUTONO-INVERNO 25/26 ✨ …"`` while its
  ``category`` is ``"PROMO PV26, EXÉ, TÉNIS, NOVA COLEÇÃO EXÉ"``. So description
  has to be scored, not just the category.
* **``category`` is a comma-separated STRING, not a list.**
* **Accents and case are inconsistent** — ``OUTONO-INVERNO``, ``outono inverno``,
  ``Outono/Inverno`` all occur, so everything is accent-folded and lowercased.
* **Season codes are written several ways** — ``PV 24``, ``PV24``, ``25/26``,
  ``25-26``. Tokens are therefore both split and re-joined (see ``_expand``) so
  each spelling matches the others.
* **``price`` and ``brand`` are frequently empty** in a catalog listing, so
  nothing here may depend on them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

#: Field weights. ``category`` is a curated taxonomy — a hit there is a stronger
#: signal than one in free-text marketing copy, which is full of cross-sell
#: mentions of products that are not this product.
FIELD_WEIGHTS = {"category": 3.0, "name": 2.0, "description": 1.0}

#: Extra credit when the hint appears as a contiguous phrase rather than as
#: scattered words — "outono inverno" in that order beats a product that merely
#: mentions "outono" in one sentence and "inverno" in another.
PHRASE_BONUS = 1.0

#: Portuguese/Spanish/English function words carry no selection signal and would
#: otherwise let any product with a "de" in it score above zero.
STOPWORDS = frozenset({
    "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "e",
    "em", "na", "no", "nas", "nos", "para", "por", "com", "the", "of", "and",
    "for", "to", "in", "colecao", "collection", "produtos", "produto",
})

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_ALPHA_DIGIT = re.compile(r"^([a-z]+)(\d+)$")
_DIGIT_ALPHA = re.compile(r"^(\d+)([a-z]+)$")


def normalize(text: Any) -> str:
    """Accent-fold, lowercase, and reduce every separator to a single space.

    ``"OUTONO-INVERNO 25/26"`` and ``"outono inverno 25 26"`` both become
    ``"outono inverno 25 26"``, which is what makes the phrase check below work
    across spellings.
    """
    if text is None:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", folded.lower()).strip()


def _split_mixed(token: str) -> list[str]:
    """``"pv24"`` → ``["pv24", "pv", "24"]`` so it also matches ``"PV 24"``."""
    for pattern in (_ALPHA_DIGIT, _DIGIT_ALPHA):
        m = pattern.match(token)
        if m:
            return [token, m.group(1), m.group(2)]
    return [token]


def _expand(text: Any) -> set[str]:
    """Every form a token might be written in: the token itself, its
    letter/digit halves, and each adjacent pair joined (``"25", "26"`` also
    yields ``"2526"``, so ``25/26`` matches ``2526``)."""
    words = normalize(text).split()
    out: set[str] = set()
    for word in words:
        out.update(_split_mixed(word))
    for left, right in zip(words, words[1:]):
        out.add(f"{left}{right}")
    return out


def hint_terms(hint: str) -> list[str]:
    """The meaningful terms of a hint, in order, deduplicated.

    Stopwords are dropped — but only if something survives. A hint that is
    *entirely* stopwords keeps its words rather than silently matching nothing.
    """
    words = [w for w in normalize(hint).split() if w]
    meaningful = [w for w in words if w not in STOPWORDS]
    kept = meaningful or words
    seen: set[str] = set()
    return [w for w in kept if not (w in seen or seen.add(w))]


def score_product(product: dict[str, Any], hint: str) -> dict[str, Any]:
    """Score one product, returning the score AND why — the ``matched`` map is
    what makes a shortlist reviewable by the human who has to approve it."""
    terms = hint_terms(hint)
    phrase = " ".join(terms)
    fields = {name: product.get(name) for name in FIELD_WEIGHTS}
    expanded = {name: _expand(value) for name, value in fields.items()}
    normalized = {name: normalize(value) for name, value in fields.items()}

    score = 0.0
    matched: dict[str, list[str]] = {}
    for term in terms:
        best_field, best_weight = None, 0.0
        for name, weight in FIELD_WEIGHTS.items():
            if term in expanded[name] and weight > best_weight:
                best_field, best_weight = name, weight
        if best_field:
            score += best_weight
            matched.setdefault(best_field, []).append(term)

    phrase_hit = None
    if len(terms) > 1:
        for name, weight in sorted(FIELD_WEIGHTS.items(), key=lambda kv: -kv[1]):
            if phrase and phrase in normalized[name]:
                score += PHRASE_BONUS * weight
                phrase_hit = name
                break

    max_score = len(terms) * FIELD_WEIGHTS["category"]
    if len(terms) > 1:
        max_score += PHRASE_BONUS * FIELD_WEIGHTS["category"]

    return {
        "id": product.get("id"),
        "name": product.get("name"),
        "sku": product.get("sku"),
        "url": product.get("url"),
        "image_url": product.get("image_url") or product.get("image") or None,
        "price": product.get("price") or product.get("formatted_price") or None,
        "score": round(score / max_score, 4) if max_score else 0.0,
        "matched": matched,
        "matched_phrase_in": phrase_hit,
    }


def filter_products(products: Iterable[dict[str, Any]], hint: str, *,
                    limit: int = 10, min_score: float = 0.2) -> dict[str, Any]:
    """Rank ``products`` against ``hint``.

    Returns the shortlist plus the near-misses just below the cut, so a human
    reviewing it can see what was *almost* included rather than only what was.
    Ties break on catalog order, never at random — same input, same output.
    """
    items = [p for p in (products or []) if isinstance(p, dict)]
    terms = hint_terms(hint)
    if not terms:
        return {
            "hint": hint,
            "terms": [],
            "considered": len(items),
            "shortlist": [],
            "near_misses": [],
            "error": "hint is empty — nothing to match on.",
        }

    scored = [score_product(p, hint) for p in items]
    ranked = sorted(scored, key=lambda s: -s["score"])
    above = [s for s in ranked if s["score"] >= min_score]
    below = [s for s in ranked if 0 < s["score"] < min_score]

    shortlist = above[:limit]
    without_image = [s["id"] for s in shortlist if not s["image_url"]]
    return {
        "hint": hint,
        "terms": terms,
        "considered": len(items),
        "min_score": min_score,
        "shortlist": shortlist,
        "near_misses": below[:limit],
        "matched_total": len(above),
        "products_without_image_url": without_image,
        "next_step": (
            "Show this shortlist to the human and get an explicit yes before "
            "calling marketing_campaign_plan. Confirming the shortlist is the "
            "main path, not a fallback."
        ),
    }
