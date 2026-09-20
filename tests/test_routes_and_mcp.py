"""The HTTP + MCP surface, driven through a real ASGI client.

Also pins the two architectural invariants that are easy to erode by accident:
this app serves no tool that creates a campaign, and none that reads a catalog.
"""

import json

import pytest
from fastapi.testclient import TestClient

from marketing_app.ledger import Ledger
from marketing_app.mcp.http_handler import TOOLS_SCHEMA
from marketing_app.routes import build_routes

TEST_TOKEN = "sk-test-not-a-real-token"  # nosec B105 - obvious placeholder, never a credential
#: The token lives in the secret store, not in config — see config.py's module
#: docstring — so it is never part of this dict, only of FakeCtx.secrets below.
CONFIG = {
    "meta_ad_account_id": "act_000000000000000",
    "meta_page_id": "100000000000000",
    "default_daily_budget_minor": 500,
    "default_country": "PT",
    "default_currency": "EUR",
}
PRODUCTS = [
    {"id": 1, "name": "Bota", "url": "https://example.test/1",
     "image_url": "https://example.test/1.png", "category": "BOTAS, OUTONO-INVERNO"},
    {"id": 2, "name": "Ténis", "url": "https://example.test/2",
     "image_url": "https://example.test/2.png", "category": "TÉNIS, OUTONO-INVERNO"},
]


class FakeSecrets:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def read(self, key):
        return self.store.get(key)

    def write(self, key, value):
        self.store[key] = value
        return {"key": key, "written": True}

    def delete(self, key):
        removed = key in self.store
        self.store.pop(key, None)
        return {"key": key, "deleted": removed}

    def keys(self):
        return list(self.store)


class FakeCtx:
    def __init__(self, package_dir, config=None, token=None):
        self.package_dir = package_dir
        self.config = dict(config or {})
        self.secrets = FakeSecrets({"meta_access_token": token} if token else {})


@pytest.fixture
def client(tmp_path):
    ctx = FakeCtx(str(tmp_path), config=dict(CONFIG), token=TEST_TOKEN)
    app = build_routes(ctx, Ledger(str(tmp_path / "data")))
    with TestClient(app) as c:
        c.aw_ctx = ctx
        c.aw_config = ctx.config
        yield c


def call(client, tool, **arguments):
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    assert response.status_code == 200
    return response.json()["result"]


def payload(result):
    return json.loads(result["content"][0]["text"].split("\n\nSend this link")[0])


# --- architectural invariants ----------------------------------------------

def test_no_tool_creates_a_campaign():
    """Creating campaigns is Meta's hosted MCP. If a tool here ever does it,
    the PAUSED-only safety model has been bypassed."""
    names = [t["name"] for t in TOOLS_SCHEMA]
    assert not [n for n in names if "create" in n or "launch" in n or "activate" in n]


def test_no_tool_reads_a_catalog():
    """Reading the catalog is the store app's job, with the store's credential.
    This app must never grow one."""
    names = [t["name"] for t in TOOLS_SCHEMA]
    assert not [n for n in names if "catalog" in n or "search" in n or "fetch" in n]


def test_the_advertised_tools_are_exactly_the_manifest_ones(client):
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert names == {
        "marketing_filter_products", "marketing_campaign_plan",
        "marketing_build_creative", "marketing_record_campaign",
        "marketing_list_campaigns",
    }


# --- protocol ---------------------------------------------------------------

def test_initialize_and_notification_handling(client):
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init.json()["result"]["serverInfo"]["name"] == "aw-app-marketing"

    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202


def test_batched_requests_return_a_list(client):
    response = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize"},
    ])
    assert [r["id"] for r in response.json()] == [1, 2]


def test_unknown_method_and_unknown_tool(client):
    unknown = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert unknown.json()["error"]["code"] == -32601
    assert call(client, "marketing_nope")["isError"] is True


def test_get_on_the_mcp_endpoint_is_405(client):
    assert client.get("/mcp").status_code == 405


# --- tools end to end -------------------------------------------------------

def test_filter_then_plan_then_creative_then_record(client):
    shortlist = payload(call(client, "marketing_filter_products",
                             products=PRODUCTS, hint="coleção outono inverno"))
    assert [p["id"] for p in shortlist["shortlist"]] == [1, 2]

    plan = payload(call(client, "marketing_campaign_plan",
                        brief="Outono/Inverno 25/26", products=PRODUCTS))
    assert plan["status"] == "PAUSED"

    creative = payload(call(client, "marketing_build_creative", products=PRODUCTS))
    assert creative["cards"] == 2

    recorded = call(client, "marketing_record_campaign", plan=plan,
                    meta_ids={"campaign_id": "120000000000001"})
    assert payload(recorded)["campaign_id"] == "120000000000001"
    # The hard stop lives in the tool result itself, not only in the skill.
    text = recorded["content"][0]["text"]
    assert "STOP" in text and "never by this agent" in text

    listed = payload(call(client, "marketing_list_campaigns"))
    assert listed["total"] == 1


def test_tool_refusals_surface_as_isError_not_exceptions(client):
    no_budget = call(client, "marketing_campaign_plan", brief="x", products=PRODUCTS,
                     daily_budget=0, country="")
    client.aw_config.update({"default_daily_budget_minor": 0, "default_country": ""})
    no_budget = call(client, "marketing_campaign_plan", brief="x", products=PRODUCTS)
    assert no_budget["isError"] is True and "daily_budget" in no_budget["content"][0]["text"]

    no_image = call(client, "marketing_build_creative",
                    products=[{"id": 1, "name": "a", "url": "u"}, {"id": 2, "name": "b", "url": "u"}])
    assert no_image["isError"] is True and "image_url" in no_image["content"][0]["text"]

    no_campaign_id = call(client, "marketing_record_campaign", meta_ids={})
    assert no_campaign_id["isError"] is True


# --- status -----------------------------------------------------------------

def test_status_reports_configured(client):
    body = client.get("/status").json()
    assert body["configured"] is True
    assert body["meta_upstream_enabled"] is True
    assert len(body["tools"]) == 5


def test_status_names_every_missing_field(tmp_path):
    """Silent degradation is this workspace's characteristic failure — an
    unconfigured app answering 200 to everything looks exactly like a working
    one unless it says so."""
    app = build_routes(FakeCtx(str(tmp_path)), Ledger(str(tmp_path / "data")))
    with TestClient(app) as client:
        body = client.get("/status").json()
    assert body["configured"] is False
    assert set(body["missing"]) == {"meta_access_token", "meta_ad_account_id", "meta_page_id"}
    assert "Not configured" in body["detail"]


def test_config_is_re_read_not_snapshotted(client):
    """A token pasted after activation has to take effect without a restart."""
    assert client.get("/status").json()["meta_upstream_enabled"] is True
    client.aw_ctx.secrets.delete("meta_access_token")
    assert client.get("/status").json()["meta_upstream_enabled"] is False


def test_settings_writes_the_token_to_secrets_not_config(client):
    """The whole point of this route: the token must never reach ctx.config."""
    resp = client.post("/settings", json={"meta_access_token": "another-test-token"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert client.aw_ctx.secrets.read("meta_access_token") == "another-test-token"
    assert "meta_access_token" not in client.aw_ctx.config


def test_settings_rejects_an_empty_token(client):
    resp = client.post("/settings", json={"meta_access_token": "  "})
    assert resp.status_code == 400


def test_logout_clears_the_token(client):
    assert client.get("/status").json()["meta_upstream_enabled"] is True
    resp = client.post("/logout")
    assert resp.status_code == 200
    assert resp.json()["meta_upstream_enabled"] is False
    assert client.get("/status").json()["meta_upstream_enabled"] is False
