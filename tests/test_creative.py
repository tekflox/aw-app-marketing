"""The carousel builder — Meta's limits enforced offline, and the two refusals
it must not be talked out of: no media, and promotional copy lifted verbatim."""

import pytest

from marketing_app.creative import CreativeError, build_creative, clean_copy

CONFIG = {"meta_page_id": "100000000000000", "meta_instagram_actor_id": "200000000000000"}


def product(pid, **overrides):
    base = {
        "id": pid,
        "name": f"Bota {pid}",
        "url": f"https://example.test/p/{pid}",
        "image_url": f"https://example.test/p/{pid}.png",
        "description": "Cano alto em pele.",
    }
    base.update(overrides)
    return base


PAIR = [product(1), product(2)]


def test_builds_a_carousel_story_spec():
    result = build_creative(PAIR, config=CONFIG)
    story = result["object_story_spec"]
    assert story["page_id"] == "100000000000000"
    assert len(story["link_data"]["child_attachments"]) == 2
    card = story["link_data"]["child_attachments"][0]
    assert card["image_url"] == "https://example.test/p/1.png"
    assert card["call_to_action"]["type"] == "SHOP_NOW"


def test_refuses_when_a_product_has_no_image_url():
    """The verified blocker: the store's catalog exposes no image field at all.
    Guessing a CDN path is how you publish an ad with the wrong shoe in it."""
    with pytest.raises(CreativeError, match="image_url"):
        build_creative([product(1), product(2, image_url=None)], config=CONFIG)


def test_the_refusal_names_the_offending_products():
    """An opaque Graph API error minutes later tells you nothing about WHICH of
    ten products was wrong."""
    with pytest.raises(CreativeError, match=r"\[2\]"):
        build_creative([product(1), product(2, image_url="")], config=CONFIG)


def test_enforces_metas_carousel_card_bounds():
    with pytest.raises(CreativeError, match="at least 2"):
        build_creative([product(1)], config=CONFIG)
    with pytest.raises(CreativeError, match="at most 10"):
        build_creative([product(i) for i in range(11)], config=CONFIG)


def test_refuses_without_a_page_id():
    with pytest.raises(CreativeError, match="meta_page_id"):
        build_creative(PAIR, config={})


def test_refuses_products_with_no_url_or_no_name():
    with pytest.raises(CreativeError, match="no url"):
        build_creative([product(1), product(2, url="")], config=CONFIG)
    with pytest.raises(CreativeError, match="no name"):
        build_creative([product(1), product(2, name="")], config=CONFIG)


def test_rejects_an_unknown_call_to_action():
    with pytest.raises(CreativeError, match="cta"):
        build_creative(PAIR, config=CONFIG, cta="PLEASE_BUY")


def test_instagram_actor_is_attached_only_for_instagram_placements():
    assert "instagram_actor_id" in build_creative(PAIR, config=CONFIG)["object_story_spec"]
    fb_only = build_creative(PAIR, config=CONFIG, platform="facebook")
    assert "instagram_actor_id" not in fb_only["object_story_spec"]


# --- the promotional-copy hazard (§6-bis) -----------------------------------

def test_strips_an_expired_promotion_from_a_description():
    """Real catalog copy. Publishing it advertises an offer that ended in 2025 —
    an ads-policy problem, not just a quality one."""
    text = "PROMOÇÃO Válida de 09/06/2023 a 30/10/2025. Bota de cano alto."
    assert clean_copy(text, max_length=30) == "Bota de cano alto"


def test_strips_a_coupon_code():
    text = "Use o cupão MIMINHOCRISPAL. Pele legítima."
    assert clean_copy(text, max_length=30) == "Pele legítima"


def test_returns_nothing_rather_than_inventing_when_all_copy_is_promotional():
    assert clean_copy("50% OFF! Use o código XPTO. Válido até 30/10.", max_length=30) == ""


def test_card_omits_the_description_when_none_of_it_is_usable():
    promo = product(1, description="PROMOÇÃO 50% desconto com o cupão XPTO")
    result = build_creative([promo, product(2)], config=CONFIG)
    assert "description" not in result["object_story_spec"]["link_data"]["child_attachments"][0]
    assert result["dropped_copy"] == [1]


def test_rejects_promotional_primary_text_instead_of_publishing_it():
    with pytest.raises(CreativeError, match="promotional"):
        build_creative(PAIR, config=CONFIG, primary_text="Aproveite 50% de desconto!")


def test_rejects_a_promotional_headline():
    with pytest.raises(CreativeError, match="promotional"):
        build_creative(PAIR, config=CONFIG, headline="SALDOS até 70%")


def test_accepts_evergreen_copy():
    result = build_creative(PAIR, config=CONFIG,
                            headline="Nova coleção", primary_text="Conforto o dia todo.")
    link_data = result["object_story_spec"]["link_data"]
    assert link_data["name"] == "Nova coleção"
    assert link_data["message"] == "Conforto o dia todo"


def test_long_copy_is_cut_on_a_word_boundary_not_mid_word():
    assert clean_copy("Conforto absoluto o dia inteiro", max_length=12) == "Conforto"
