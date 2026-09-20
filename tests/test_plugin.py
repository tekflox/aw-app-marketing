"""The plugin's writes of ``mcp.json`` — on activate, on config save, and via
``routes.py``'s ``/settings``/``/logout``.

These exist because the failure they guard against is invisible: an app whose
``marketing`` upstream vanished still loads, still serves its routes, and simply
has no MCP tools.
"""

import asyncio
import json
import os

import pytest

from marketing_app.mcp import self_register
from marketing_app.plugin import MarketingAppPlugin


class FakeRoutes:
    def __init__(self):
        self.registered = []

    def register(self, subapp):
        self.registered.append(subapp)


class FakeSecrets:
    def __init__(self):
        self.store = {}

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
    def __init__(self, package_dir, config=None):
        self.package_dir = package_dir
        self.config = dict(config or {})
        self.routes = FakeRoutes()
        self.secrets = FakeSecrets()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "test-api-key-not-real")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_PORT", "9030")
    return FakeCtx(str(tmp_path))


def servers(ctx):
    with open(os.path.join(ctx.package_dir, "mcp.json")) as fh:
        return json.load(fh)["mcpServers"]


def test_activate_registers_routes_and_writes_both_upstreams(ctx):
    asyncio.run(MarketingAppPlugin().activate(ctx))
    assert len(ctx.routes.registered) == 1
    assert set(servers(ctx)) == {"marketing", "meta-ads"}


def test_activate_leaves_meta_disabled_until_a_token_exists(ctx):
    asyncio.run(MarketingAppPlugin().activate(ctx))
    assert servers(ctx)["meta-ads"]["enabled"] is False


def test_saving_a_token_via_settings_rewrites_mcp_json_immediately(ctx):
    """The token travels through POST /settings straight to ctx.secrets, never
    through the generic config path — so /settings has to rewrite mcp.json
    itself rather than relying on on_config_saved to do it."""
    from fastapi.testclient import TestClient

    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    assert servers(ctx)["meta-ads"]["enabled"] is False

    with TestClient(ctx.routes.registered[0]) as client:
        resp = client.post("/settings", json={"meta_access_token": "sk-test-not-a-real-token"})
        assert resp.status_code == 200

    meta = servers(ctx)["meta-ads"]
    assert meta["enabled"] is True
    assert meta["headers"]["Authorization"].endswith("sk-test-not-a-real-token")
    assert ctx.config == {}, "the token must never land in plain config"


def test_on_config_saved_does_not_touch_the_token(ctx):
    """Non-token config fields still trigger the hook, but it must not be what
    makes a token take effect — that is /settings's job now."""
    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    ctx.config["meta_ad_account_id"] = "act_000000000000000"
    asyncio.run(plugin.on_config_saved(ctx))
    assert servers(ctx)["meta-ads"]["enabled"] is False


def test_logout_disables_the_meta_upstream(ctx):
    from fastapi.testclient import TestClient

    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    ctx.secrets.write("meta_access_token", "sk-test-not-a-real-token")  # nosec B105
    plugin._write_mcp_json(ctx)
    assert servers(ctx)["meta-ads"]["enabled"] is True

    with TestClient(ctx.routes.registered[0]) as client:
        resp = client.post("/logout")
        assert resp.status_code == 200

    assert servers(ctx)["meta-ads"]["enabled"] is False
    assert ctx.secrets.read("meta_access_token") is None


def test_reactivation_is_idempotent(ctx):
    """activate() re-runs on every boot and after every update."""
    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    first = servers(ctx)
    asyncio.run(plugin.activate(ctx))
    assert servers(ctx) == first


def test_the_mounted_app_serves_the_live_config(ctx):
    """Routes are built against ``ctx`` directly, not a snapshot, so a later
    token save is visible to the HTTP surface without re-registering anything."""
    from fastapi.testclient import TestClient

    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    with TestClient(ctx.routes.registered[0]) as client:
        assert client.get("/status").json()["meta_upstream_enabled"] is False
        ctx.secrets.write("meta_access_token", "sk-test-not-a-real-token")  # nosec B105
        assert client.get("/status").json()["meta_upstream_enabled"] is True


def test_deactivate_is_safe(ctx):
    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    asyncio.run(plugin.deactivate())


def test_port_comes_from_the_environment(ctx, monkeypatch):
    monkeypatch.setenv("AW_PORT", "9999")
    asyncio.run(MarketingAppPlugin().activate(ctx))
    assert ":9999/" in servers(ctx)["marketing"]["url"]

    monkeypatch.delenv("AW_PORT")
    os.remove(self_register.mcp_json_path(ctx.package_dir))
    asyncio.run(MarketingAppPlugin().activate(ctx))
    assert ":9030/" in servers(ctx)["marketing"]["url"]
