"""The audit ledger — append-only, idempotent, and unreadable rows must not make
the whole log unreadable."""

import json

import pytest

from marketing_app.ledger import Ledger, ads_manager_url

PLAN = {
    "brief": "Outono/Inverno 25/26",
    "status": "PAUSED",
    "currency": "EUR",
    "campaign": {"objective": "OUTCOME_TRAFFIC"},
    "ad_set": {"daily_budget": 500},
    "products": [{"id": 1}, {"id": 2}],
}
META_IDS = {"campaign_id": "120000000000001", "ad_set_id": "120000000000002",
            "ad_id": "120000000000003", "ad_account_id": "act_000000000000000"}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "data"))


def test_records_a_campaign_with_its_ads_manager_link(ledger):
    entry = ledger.record(PLAN, META_IDS)
    assert entry["campaign_id"] == "120000000000001"
    assert entry["products"] == [1, 2]
    assert entry["status"] == "PAUSED"
    assert "selected_campaign_ids=120000000000001" in entry["ads_manager_url"]
    assert entry["already_recorded"] is False


def test_recording_the_same_campaign_twice_does_not_duplicate_it(ledger):
    """Idempotency is what makes the flow safe to resume after an agent loses
    its place — re-running ad creation costs real money."""
    ledger.record(PLAN, META_IDS)
    again = ledger.record(PLAN, META_IDS)
    assert again["already_recorded"] is True
    assert ledger.list()["total"] == 1


def test_requires_a_campaign_id(ledger):
    """Record what Meta actually created, not what was planned."""
    with pytest.raises(ValueError, match="campaign_id"):
        ledger.record(PLAN, {"ad_id": "x"})


def test_metas_own_permalink_wins_over_the_derived_link(ledger):
    """Meta has moved the Ads Manager host before; a permalink it returned
    itself is the source of truth."""
    entry = ledger.record(PLAN, {**META_IDS, "permalink": "https://meta.test/own-link"})
    assert entry["ads_manager_url"] == "https://meta.test/own-link"


def test_ad_account_id_falls_back_to_the_plan_then_to_config(ledger):
    plan = {**PLAN, "ad_account_id": "act_111"}
    assert "act_111" in ledger.record(plan, {"campaign_id": "a"})["ads_manager_url"]
    assert "act_222" in ledger.record(
        {}, {"campaign_id": "b"}, config={"meta_ad_account_id": "act_222"}
    )["ads_manager_url"]


def test_no_link_when_there_is_no_account(ledger):
    assert ledger.record({}, {"campaign_id": "c"})["ads_manager_url"] is None


def test_ads_manager_url_normalises_a_bare_account_id():
    assert "act_123" in ads_manager_url("123", "456")
    assert ads_manager_url(None, "456") is None


def test_is_append_only(ledger):
    ledger.record(PLAN, META_IDS)
    ledger.record(PLAN, {**META_IDS, "campaign_id": "120000000000009"})
    with open(ledger.path) as fh:
        lines = [line for line in fh if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["campaign_id"] == "120000000000001"


def test_listing_is_most_recent_first_and_limited(ledger):
    for i in range(5):
        ledger.record(PLAN, {**META_IDS, "campaign_id": str(i)})
    listed = ledger.list(limit=2)
    assert [c["campaign_id"] for c in listed["campaigns"]] == ["4", "3"]
    assert listed["total"] == 5


def test_an_unreadable_line_does_not_break_the_whole_log(ledger):
    """One corrupt row must not make the audit trail unreadable."""
    ledger.record(PLAN, META_IDS)
    with open(ledger.path, "a") as fh:
        fh.write("{not json\n")
    ledger.record(PLAN, {**META_IDS, "campaign_id": "120000000000009"})
    assert ledger.list()["total"] == 2


def test_an_absent_ledger_lists_empty_rather_than_failing(ledger):
    assert ledger.list() == {"total": 0, "returned": 0, "campaigns": [],
                             "ledger_path": ledger.path}


def test_data_dir_defaults_under_the_workspace_home(monkeypatch):
    """Package-relative state is deleted wholesale on app update — for an audit
    log that means it silently empties exactly when someone needs it."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", "/tmp/aw-home-test")
    assert Ledger().path == "/tmp/aw-home-test/data/marketing/campaigns.jsonl"


def test_blank_lines_are_skipped(ledger):
    """A partially-flushed append or a stray newline must not become an entry."""
    ledger.record(PLAN, META_IDS)
    with open(ledger.path, "a") as fh:
        fh.write("\n\n")
    assert ledger.list()["total"] == 1
