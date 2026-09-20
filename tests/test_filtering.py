"""Product scoring, against the shapes the REAL catalog was observed to have on
2026-09-19 — collection only in the free-text description, comma-separated
category string, inconsistent accents/case, empty price and brand fields."""

from marketing_app.filtering import filter_products, hint_terms, normalize, score_product

# Modelled on product 59340 as actually returned by the store's get_catalog.
AUTUMN = {
    "id": 59340,
    "name": "Ténis EXÉ Branco",
    "sku": "EXE-001",
    "url": "https://example.test/p/59340",
    "category": "PROMO PV26, EXÉ, TÉNIS, NOVA COLEÇÃO EXÉ",
    "description": "🌟 NOVA COLEÇÃO OUTONO-INVERNO 25/26 ✨ Conforto o dia todo.",
    "price": "",
}
SANDAL = {
    "id": 1001,
    "name": "Sandália Verão",
    "url": "https://example.test/p/1001",
    "category": "SANDÁLIAS, VERÃO",
    "description": "Leve e fresca para os dias quentes.",
}
BOOT = {
    "id": 1002,
    "name": "Bota Outono Inverno",
    "url": "https://example.test/p/1002",
    "category": "BOTAS, OUTONO-INVERNO",
    "description": "Cano alto.",
}


def test_normalize_folds_accents_case_and_separators():
    assert normalize("OUTONO-INVERNO 25/26") == "outono inverno 25 26"
    assert normalize("EXÉ") == "exe"


def test_hint_terms_drop_stopwords():
    assert hint_terms("a coleção de outono e inverno") == ["outono", "inverno"]


def test_hint_of_only_stopwords_keeps_its_words():
    """Dropping everything would silently match nothing at all."""
    assert hint_terms("de a o") == ["de", "a", "o"]


def test_matches_a_collection_that_only_exists_in_the_description():
    """The catalog's real shape: the season is in free text, the taxonomy says
    something else entirely. Scoring category alone would miss this product."""
    scored = score_product(AUTUMN, "coleção outono inverno")
    assert scored["score"] > 0
    assert set(scored["matched"].get("description", [])) == {"outono", "inverno"}


def test_category_hit_outweighs_a_description_hit():
    boot = score_product(BOOT, "outono inverno")
    autumn = score_product(AUTUMN, "outono inverno")
    assert boot["score"] > autumn["score"]


def test_season_codes_match_across_spellings():
    """PV 24 / PV24 and 25/26 / 25-26 / 2526 are the same season."""
    for hint in ("25/26", "25-26", "2526"):
        assert score_product(AUTUMN, hint)["score"] > 0, hint

    product = {"id": 1, "category": "PV 24"}
    for hint in ("PV24", "pv 24"):
        assert score_product(product, hint)["score"] > 0, hint


def test_accents_and_case_are_irrelevant():
    assert score_product(AUTUMN, "EXÉ")["score"] == score_product(AUTUMN, "exe")["score"] > 0


def test_unrelated_products_score_zero():
    assert score_product(SANDAL, "outono inverno")["score"] == 0


def test_shortlist_is_ranked_and_reports_near_misses():
    result = filter_products([SANDAL, AUTUMN, BOOT], "outono inverno")
    assert [p["id"] for p in result["shortlist"]] == [1002, 59340]
    assert result["considered"] == 3
    assert all("matched" in p for p in result["shortlist"])


def test_is_deterministic():
    """Same catalog + same hint → same shortlist, which is what makes the human
    approval step mean anything."""
    products = [SANDAL, AUTUMN, BOOT]
    assert filter_products(products, "outono inverno") == filter_products(products, "outono inverno")


def test_flags_products_with_no_image_url():
    """The §6-bis blocker, surfaced at selection time rather than at creative
    time — every product in this fixture lacks one, like the real catalog."""
    result = filter_products([AUTUMN, BOOT], "outono inverno")
    assert result["products_without_image_url"] == [1002, 59340]


def test_limit_and_min_score_are_honoured():
    result = filter_products([SANDAL, AUTUMN, BOOT], "outono inverno", limit=1)
    assert len(result["shortlist"]) == 1
    assert result["matched_total"] == 2

    # A product whose category carries every term AS A PHRASE scores 1.0; one
    # that only carries them in free text falls below and becomes a near-miss.
    strict = filter_products([SANDAL, AUTUMN, BOOT], "outono inverno", min_score=0.99)
    assert [p["id"] for p in strict["shortlist"]] == [1002]
    assert [p["id"] for p in strict["near_misses"]] == [59340]


def test_empty_hint_is_an_explicit_error_not_an_empty_match():
    result = filter_products([AUTUMN], "")
    assert "error" in result and result["shortlist"] == []


def test_non_dict_entries_are_ignored():
    result = filter_products([AUTUMN, None, "nope"], "outono inverno")
    assert result["considered"] == 1
