"""The audit ledger — append-only, idempotent, and unreadable rows must not make
the whole log unreadable."""

import json
import os

import pytest

from marketing_app.ledger import ENTRY_TYPE_ACTIVATION, ENTRY_TYPE_CREATION, Ledger, ads_manager_url

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


# --- entry_type: creation vs. activation rows -------------------------------

def test_record_writes_a_creation_row(ledger):
    assert ledger.record(PLAN, META_IDS)["entry_type"] == ENTRY_TYPE_CREATION


def test_record_event_requires_a_campaign_id(ledger):
    with pytest.raises(ValueError, match="campaign_id"):
        ledger.record_event("", "activation_denied")


def test_record_event_appends_an_activation_row(ledger):
    event = ledger.record_event("c1", "activation_denied",
                                ad_account_id="act_1", notes="approval denied")
    assert event["entry_type"] == ENTRY_TYPE_ACTIVATION
    assert event["campaign_id"] == "c1"
    assert event["event"] == "activation_denied"
    assert event["notes"] == "approval denied"
    with open(ledger.path) as fh:
        assert len([line for line in fh if line.strip()]) == 1


def test_an_activation_row_never_answers_a_creation_lookup(ledger):
    """The bug this guards against: a same-campaign_id activation row must
    not be handed back to record()'s idempotency check as the creation row."""
    ledger.record(PLAN, META_IDS)
    ledger.record_event(META_IDS["campaign_id"], "activation_success")
    found = ledger.find(META_IDS["campaign_id"])
    assert found["entry_type"] == ENTRY_TYPE_CREATION

    activation_row = ledger.find(META_IDS["campaign_id"], entry_type=ENTRY_TYPE_ACTIVATION)
    assert activation_row["event"] == "activation_success"


def test_a_row_written_before_entry_type_existed_is_treated_as_creation(ledger):
    """Backward compatibility: old rows carry no ``entry_type`` key at all."""
    legacy = {**META_IDS, "recorded_at": "2026-01-01T00:00:00+00:00", "brief": "x"}
    os.makedirs(ledger.data_dir, exist_ok=True)
    with open(ledger.path, "a") as fh:
        fh.write(json.dumps(legacy) + "\n")
    assert ledger.find(META_IDS["campaign_id"]) == legacy
