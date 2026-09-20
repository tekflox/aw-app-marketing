"""The plugin's two writes of ``mcp.json`` — on activate and on config save.

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


class FakeCtx:
    def __init__(self, package_dir, config=None):
        self.package_dir = package_dir
        self.config = dict(config or {})
        self.routes = FakeRoutes()


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


def test_saving_a_token_rewrites_mcp_json_immediately(ctx):
    """The runtime calls on_config_saved BEFORE reloading the gateway, which is
    what makes a pasted token take effect now instead of on the next boot."""
    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    assert servers(ctx)["meta-ads"]["enabled"] is False

    ctx.config["meta_access_token"] = "sk-test-not-a-real-token"  # nosec B105
    asyncio.run(plugin.on_config_saved(ctx))

    meta = servers(ctx)["meta-ads"]
    assert meta["enabled"] is True
    assert meta["headers"]["Authorization"].endswith("sk-test-not-a-real-token")


def test_reactivation_is_idempotent(ctx):
    """activate() re-runs on every boot and after every update."""
    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    first = servers(ctx)
    asyncio.run(plugin.activate(ctx))
    assert servers(ctx) == first


def test_the_mounted_app_serves_the_live_config(ctx):
    """Routes are built with a callable, not a snapshot, so a later token save
    is visible to the HTTP surface without re-registering anything."""
    from fastapi.testclient import TestClient

    plugin = MarketingAppPlugin()
    asyncio.run(plugin.activate(ctx))
    with TestClient(ctx.routes.registered[0]) as client:
        assert client.get("/status").json()["meta_upstream_enabled"] is False
        ctx.config["meta_access_token"] = "sk-test-not-a-real-token"  # nosec B105
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
