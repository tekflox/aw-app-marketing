"""The ``mcp.json`` writer — the one piece of this app whose failure is silent.

A missing ``marketing`` entry means the gateway serves zero of this app's tools;
a Meta entry enabled without a token means a connected upstream that 401s and
also serves zero tools. Neither shows up anywhere but a container log, so both
are asserted here.
"""

import json
import os

import pytest

from marketing_app.mcp import self_register

TEST_TOKEN = "sk-test-not-a-real-token"  # nosec B105 - obvious placeholder, never a credential


@pytest.fixture
def package_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "test-api-key-not-real")
    return str(tmp_path)


def _read(package_dir):
    with open(self_register.mcp_json_path(package_dir)) as fh:
        return json.load(fh)


def test_writes_both_upstreams_in_one_file(package_dir):
    """Both entries in ONE write — a second writer is what silently erases the
    first one's work (see the module docstring)."""
    assert self_register.register_self(package_dir, 9030, {"meta_access_token": TEST_TOKEN})

    servers = _read(package_dir)["mcpServers"]
    assert set(servers) == {"marketing", "meta-ads"}
    assert servers["marketing"]["url"].endswith("/api/apps/marketing/mcp")
    assert servers["marketing"]["enabled"] is True
    assert servers["meta-ads"]["url"] == "https://mcp.facebook.com/ads"


def test_own_entry_carries_the_workspace_api_key(package_dir):
    """Tier-1 routes are IdentityGuard-gated — without this header the gateway
    cannot reach our own /mcp endpoint at all."""
    self_register.register_self(package_dir, 9030, {})
    assert _read(package_dir)["mcpServers"]["marketing"]["headers"] == {
        "X-Api-Key": "test-api-key-not-real"
    }


def test_own_entry_omits_headers_when_there_is_no_api_key(package_dir, monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_API_KEY", raising=False)
    self_register.register_self(package_dir, 9030, {})
    assert "headers" not in _read(package_dir)["mcpServers"]["marketing"]


def test_meta_upstream_is_disabled_and_headerless_without_a_token(package_dir):
    """An upstream with an empty Bearer connects, 401s and serves zero tools —
    which reads as a broken app rather than an unconfigured one."""
    self_register.register_self(package_dir, 9030, {"meta_access_token": "   "})
    meta = _read(package_dir)["mcpServers"]["meta-ads"]
    assert meta["enabled"] is False
    assert "headers" not in meta


def test_meta_upstream_is_enabled_once_a_token_exists(package_dir):
    self_register.register_self(package_dir, 9030, {"meta_access_token": TEST_TOKEN})
    meta = _read(package_dir)["mcpServers"]["meta-ads"]
    assert meta["enabled"] is True
    assert meta["headers"] == {"Authorization": f"Bearer {TEST_TOKEN}"}


def test_rewrites_when_the_token_changes(package_dir):
    self_register.register_self(package_dir, 9030, {})
    assert self_register.register_self(package_dir, 9030, {"meta_access_token": TEST_TOKEN}) is True
    assert _read(package_dir)["mcpServers"]["meta-ads"]["enabled"] is True


def test_does_not_rewrite_an_already_correct_file(package_dir):
    """Activation re-runs on every boot; churning the file would reload the
    gateway for nothing each time."""
    config = {"meta_access_token": TEST_TOKEN}
    assert self_register.register_self(package_dir, 9030, config) is True
    assert self_register.register_self(package_dir, 9030, config) is False


def test_replaces_a_corrupted_file(package_dir):
    with open(self_register.mcp_json_path(package_dir), "w") as fh:
        fh.write("{not json")
    assert self_register.register_self(package_dir, 9030, {}) is True
    assert set(_read(package_dir)["mcpServers"]) == {"marketing", "meta-ads"}


def test_file_is_not_world_readable(package_dir):
    """It holds the workspace API key and the Meta token."""
    self_register.register_self(package_dir, 9030, {"meta_access_token": TEST_TOKEN})
    mode = os.stat(self_register.mcp_json_path(package_dir)).st_mode & 0o777
    assert mode == 0o600


def test_no_package_dir_is_a_no_op_not_a_crash(tmp_path):
    """A bare dev run has nothing to write into; that must not fail activation."""
    assert self_register.register_self(str(tmp_path / "nope"), 9030, {}) is False


def test_an_unwritable_dir_is_a_warning_not_a_crash(package_dir, monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(self_register.os, "open", boom)
    assert self_register.register_self(package_dir, 9030, {}) is False


def test_the_repo_ships_no_mcp_template():
    """The whole single-writer design rests on ``mcp_template.render()``
    short-circuiting on its ``os.path.isfile`` guard. A template appearing in
    this repo would make the runtime overwrite our file after every activate and
    every config save — silently."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(repo_root, "mcp.template.json"))
