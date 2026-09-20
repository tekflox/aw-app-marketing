"""The campaign plan. Two properties matter more than the rest: it never
invents a number that costs money, and everything it emits is PAUSED."""

import pytest

from marketing_app.planner import PAUSED, PlanError, campaign_plan

CONFIG = {
    "meta_access_token": "sk-test-not-a-real-token",  # nosec B105 - placeholder
    "meta_ad_account_id": "act_000000000000000",
    "meta_page_id": "100000000000000",
    "meta_instagram_actor_id": "200000000000000",
    "default_daily_budget_minor": 500,
    "default_country": "PT",
    "default_currency": "EUR",
}
PRODUCTS = [
    {"id": 1, "name": "Bota", "url": "https://example.test/1", "image_url": "https://example.test/1.png"},
    {"id": 2, "name": "Ténis", "url": "https://example.test/2", "image_url": "https://example.test/2.png"},
]


def test_everything_is_paused():
    """PAUSED is the only brake between an agent and real ad spend — the
    gateway's approval gate does not cover upstream tool calls."""
    plan = campaign_plan("Outono/Inverno", PRODUCTS, config=CONFIG)
    assert plan["status"] == PAUSED
    assert plan["campaign"]["status"] == PAUSED
    assert plan["ad_set"]["status"] == PAUSED
    assert plan["ad"]["status"] == PAUSED
    assert PAUSED in plan["human_summary"]


def test_no_status_anywhere_in_the_plan_is_active():
    plan = campaign_plan("Outono/Inverno", PRODUCTS, config=CONFIG)

    def statuses(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "status":
                    yield value
                yield from statuses(value)
        elif isinstance(node, list):
            for item in node:
                yield from statuses(item)

    assert set(statuses(plan)) == {PAUSED}


def test_falls_back_to_configured_defaults():
    plan = campaign_plan("Outono/Inverno", PRODUCTS, config=CONFIG)
    assert plan["ad_set"]["daily_budget"] == 500
    assert plan["ad_set"]["targeting"]["geo_locations"]["countries"] == ["PT"]
    assert plan["currency"] == "EUR"


def test_arguments_beat_configured_defaults():
    plan = campaign_plan("x", PRODUCTS, config=CONFIG, daily_budget=1500, country="es")
    assert plan["ad_set"]["daily_budget"] == 1500
    assert plan["ad_set"]["targeting"]["geo_locations"]["countries"] == ["ES"]


def test_refuses_to_invent_a_budget():
    """An agent guessing a daily budget is an agent guessing how much money to
    spend. It has to ask."""
    with pytest.raises(PlanError, match="daily_budget"):
        campaign_plan("x", PRODUCTS, config={"default_country": "PT"})


def test_refuses_to_invent_a_country():
    with pytest.raises(PlanError, match="country"):
        campaign_plan("x", PRODUCTS, config={"default_daily_budget_minor": 500})


def test_refuses_a_zero_or_negative_budget():
    with pytest.raises(PlanError, match="positive"):
        campaign_plan("x", PRODUCTS, config=CONFIG, daily_budget=-1)


def test_refuses_an_empty_brief_or_empty_products():
    with pytest.raises(PlanError, match="brief"):
        campaign_plan("  ", PRODUCTS, config=CONFIG)
    with pytest.raises(PlanError, match="products"):
        campaign_plan("x", [], config=CONFIG)


def test_rejects_unknown_objective_and_platform():
    with pytest.raises(PlanError, match="objective"):
        campaign_plan("x", PRODUCTS, config=CONFIG, objective="MAKE_MONEY")
    with pytest.raises(PlanError, match="platform"):
        campaign_plan("x", PRODUCTS, config=CONFIG, platform="tiktok")


def test_default_objective_is_traffic():
    """Sales optimisation silently underdelivers without a configured pixel."""
    assert campaign_plan("x", PRODUCTS, config=CONFIG)["campaign"]["objective"] == "OUTCOME_TRAFFIC"


def test_platform_selects_publisher_platforms():
    both = campaign_plan("x", PRODUCTS, config=CONFIG)["ad_set"]["targeting"]["publisher_platforms"]
    assert both == ["facebook", "instagram"]
    only_ig = campaign_plan("x", PRODUCTS, config=CONFIG, platform="instagram")
    assert only_ig["ad_set"]["targeting"]["publisher_platforms"] == ["instagram"]
    assert only_ig["creative_hint"]["instagram_actor_id"] == "200000000000000"


def test_warns_when_instagram_is_asked_for_but_not_configured():
    config = {**CONFIG, "meta_instagram_actor_id": ""}
    plan = campaign_plan("x", PRODUCTS, config=config, platform="instagram")
    assert any("meta_instagram_actor_id" in w for w in plan["warnings"])


def test_warns_but_still_plans_when_meta_is_not_configured():
    """The plan is still worth having — it is what the human reviews while
    someone goes and generates the token."""
    plan = campaign_plan("x", PRODUCTS, config={"default_daily_budget_minor": 500,
                                                "default_country": "PT"})
    assert plan["ad_set"]["daily_budget"] == 500
    assert any("Not configured" in w for w in plan["warnings"])


def test_warns_about_products_with_no_image():
    plan = campaign_plan("x", [{"id": 9, "name": "Bota", "url": "https://example.test/9"}],
                         config=CONFIG)
    assert any("image_url" in w for w in plan["warnings"])
