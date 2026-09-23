"""``marketing_activate_campaign``: the approval gate, the Graph activation,
and the ledger event it leaves behind.

Every network boundary is a fake callable, never httpx itself (``http_post``/
``http_get`` are required arguments — see ``activation.py``'s module
docstring). These tests prove the *shape* of every call this flow makes; they
cannot prove aw-backend, the Telegram approval, or the Graph API actually
behave the way the fakes script them.
"""

from __future__ import annotations

import httpx
import pytest

from marketing_app import activation
from marketing_app.activation import (
    ActivationError,
    _approval_backend,
    _budget_display,
    _build_reason,
    activate_campaign,
)
from marketing_app.ledger import ENTRY_TYPE_ACTIVATION, Ledger
from marketing_app.mcp import http_handler
from marketing_app.mcp.http_handler import handle_request

TOKEN = "sk-test-not-a-real-token"  # nosec B105 - obvious placeholder, never a credential
ENV = {
    "AW_BACKEND_URL": "https://backend.test",
    "AW_WORKSPACE": "acme",
    "AW_WORKSPACE_HOST_TOKEN": "host-token-not-real",  # nosec B105
}


@pytest.fixture(autouse=True)
def approval_env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "data"))


def _record(ledger, **overrides):
    plan = {"brief": "Outono/Inverno 25/26", "currency": "EUR",
            "ad_set": {"daily_budget": 500}}
    meta_ids = {"campaign_id": "c1", "ad_set_id": "s1", "ad_id": "a1",
                "ad_account_id": "act_1", **overrides}
    return ledger.record(plan, meta_ids)


class FakeResponse:
    def __init__(self, status_code=200, body=None, *, raises=False):
        self.status_code = status_code
        self._body = body or {}
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("response is not valid json")
        return self._body


class Scripted:
    """A queue of canned responses (or exceptions), one per call, shared by
    every ``await``. Raises loudly if a test scripts fewer than it needs —
    that mismatch is a bug in the test, not something to paper over."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


APPROVED_POST = FakeResponse(200, {"request_id": "req_1"})
APPROVED_GET = FakeResponse(200, {"status": "approved"})


# --- refusals before any network call ---------------------------------------

async def test_campaign_id_is_required(ledger):
    with pytest.raises(ActivationError, match="campaign_id is required"):
        await activate_campaign("", ledger=ledger, token=TOKEN,
                                http_post=Scripted(), http_get=Scripted())


async def test_unknown_campaign_id_refuses(ledger):
    """Nothing to build a safe approval prompt from — and this also proves
    ``find`` skips a non-matching row rather than just taking the first one."""
    _record(ledger)
    with pytest.raises(ActivationError, match="No ledger entry"):
        await activate_campaign("nope", ledger=ledger, token=TOKEN,
                                http_post=Scripted(), http_get=Scripted())


# --- the approval gate, fail-closed every way ------------------------------

async def test_missing_backend_config_fails_closed(ledger, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    _record(ledger)
    result = await activate_campaign("c1", ledger=ledger, token=TOKEN,
                                     http_post=Scripted(), http_get=Scripted())
    assert result["approved"] is False
    assert result["overall"] == "denied"
    assert "AW_BACKEND_URL" in result["detail"]
    assert result["ledger_entry"]["entry_type"] == ENTRY_TYPE_ACTIVATION
    assert result["ledger_entry"]["event"] == "activation_denied"


def test_approval_backend_names_every_missing_var(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_HOST_TOKEN", raising=False)
    base, slug, token = _approval_backend()
    assert base == ENV["AW_BACKEND_URL"]
    assert slug == "" and token == ""


async def test_approval_request_rejected_by_backend(ledger):
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=Scripted(FakeResponse(500)), http_get=Scripted(),
    )
    assert result["overall"] == "denied"
    assert "HTTP 500" in result["detail"]


async def test_approval_request_without_a_request_id_refuses(ledger):
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=Scripted(FakeResponse(200, {})), http_get=Scripted(),
    )
    assert result["overall"] == "denied"
    assert "no request_id" in result["detail"]


async def test_approval_request_network_error_fails_closed(ledger):
    """An exception must never reach the Graph call."""
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=Scripted(ConnectionError("no route to host")), http_get=Scripted(),
    )
    assert result["overall"] == "denied"
    assert "no route to host" in result["detail"]


@pytest.mark.parametrize("status", ["denied", "expired", "not_found", "error"])
async def test_terminal_poll_statuses_all_refuse(ledger, status):
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=Scripted(APPROVED_POST),
        http_get=Scripted(FakeResponse(200, {"status": status})),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["overall"] == "denied"
    assert result["detail"] == f"approval {status}"


async def test_a_non_200_poll_is_retried_not_treated_as_terminal(ledger):
    """A transport hiccup on one poll must not abort the wait — only an
    exception, or a terminal status, does that."""
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=Scripted(APPROVED_POST),
        http_get=Scripted(FakeResponse(503), APPROVED_GET),
        poll_attempts=2, poll_interval_s=0,
    )
    assert result["approved"] is True


async def test_polling_exhausts_and_times_out(ledger):
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=Scripted(APPROVED_POST),
        http_get=Scripted(FakeResponse(200, {"status": "pending"})),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["overall"] == "denied"
    assert result["detail"] == "approval timed out"


# --- approved: the Graph activation ----------------------------------------

async def test_approved_but_token_not_configured(ledger):
    _record(ledger)
    result = await activate_campaign(
        "c1", ledger=ledger, token="   ",
        http_post=Scripted(APPROVED_POST), http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["approved"] is True
    assert result["overall"] == "failed"
    assert result["results"] == []
    assert "meta_access_token" in result["detail"]
    assert result["ledger_entry"]["event"] == "activation_failed"


async def test_full_success_activates_the_whole_tree(ledger):
    entry = _record(ledger)
    posts = Scripted(APPROVED_POST, FakeResponse(200), FakeResponse(200), FakeResponse(200))
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=posts, http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["overall"] == "success"
    assert [r["entity_id"] for r in result["results"]] == ["c1", "s1", "a1"]
    assert all(r["activated"] for r in result["results"])
    assert result["fallback_needed"] is False
    assert result["fallback_hint"] is None
    assert result["ledger_entry"]["event"] == "activation_success"
    # The approval request named this campaign, its account and its budget —
    # never a free-form argument.
    approval_call = posts.calls[0]
    reason = approval_call[1]["json"]["reason"]
    assert entry["campaign_id"] in reason and "act_1" in reason and "5.00 EUR" in reason
    assert approval_call[1]["json"]["request_type"] == "agent_run"


async def test_only_the_campaign_when_no_ad_set_or_ad_was_recorded(ledger):
    _record(ledger, ad_set_id=None, ad_id=None)
    posts = Scripted(APPROVED_POST, FakeResponse(200))
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=posts, http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert [r["entity_id"] for r in result["results"]] == ["c1"]
    assert result["overall"] == "success"


async def test_partial_failure_passes_metas_message_through_literally(ledger):
    """The observed real failure: campaign activates, ad set is rejected for
    a budget reason this app must not re-validate or second-guess."""
    _record(ledger)
    meta_error = {"error": {"message": (
        "Minimum spend limit is higher than the campaign budget."
    )}}
    posts = Scripted(APPROVED_POST, FakeResponse(200), FakeResponse(400, meta_error), FakeResponse(200))
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=posts, http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["overall"] == "partial"
    failed = [r for r in result["results"] if not r["activated"]][0]
    assert failed["error"] == "Minimum spend limit is higher than the campaign budget."
    assert failed["needs_fallback"] is False
    assert result["fallback_needed"] is False


async def test_403_marks_fallback_needed_and_names_the_completion_path(ledger):
    _record(ledger, ad_set_id=None, ad_id=None)
    posts = Scripted(APPROVED_POST, FakeResponse(403, {"error": {"message": "Insufficient permission"}}))
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=posts, http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["overall"] == "failed"
    assert result["results"][0]["needs_fallback"] is True
    assert result["fallback_needed"] is True
    assert "ads_activate_entity" in result["fallback_hint"]


async def test_entity_network_exception_is_reported_not_raised(ledger):
    _record(ledger, ad_set_id=None, ad_id=None)
    posts = Scripted(APPROVED_POST, ConnectionError("reset"))
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=posts, http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["results"][0]["activated"] is False
    assert result["results"][0]["error"] == "reset"


async def test_entity_error_with_unparseable_body_falls_back_to_http_status(ledger):
    _record(ledger, ad_set_id=None, ad_id=None)
    posts = Scripted(APPROVED_POST, FakeResponse(402, raises=True))
    result = await activate_campaign(
        "c1", ledger=ledger, token=TOKEN,
        http_post=posts, http_get=Scripted(APPROVED_GET),
        poll_attempts=1, poll_interval_s=0,
    )
    assert result["results"][0]["error"] == "HTTP 402"


# --- helpers, directly ------------------------------------------------------

def test_budget_display_formats_minor_units_with_currency():
    assert _budget_display({"daily_budget": 500, "currency": "EUR"}) == "5.00 EUR"


def test_budget_display_without_currency_is_just_the_number():
    assert _budget_display({"daily_budget": 500}) == "5.00"


def test_budget_display_falls_back_when_not_a_number():
    assert _budget_display({"daily_budget": None}) == "an unspecified budget"


def test_build_reason_names_campaign_account_and_budget():
    reason = _build_reason({"brief": "Outono", "campaign_id": "c1",
                            "ad_account_id": "act_1", "daily_budget": 500, "currency": "EUR"})
    assert "Outono" in reason and "c1" in reason and "act_1" in reason and "5.00 EUR" in reason


def test_build_reason_falls_back_to_campaign_id_and_unknown_account():
    reason = _build_reason({"campaign_id": "c1", "daily_budget": None})
    assert "c1" in reason and "unknown" in reason


# --- the MCP dispatch --------------------------------------------------------

async def test_dispatch_activates_through_the_mcp_tool(ledger):
    _record(ledger, ad_set_id=None, ad_id=None)
    posts = Scripted(APPROVED_POST, FakeResponse(200))
    response = await handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "marketing_activate_campaign", "arguments": {"campaign_id": "c1"}}},
        token=TOKEN, ledger=ledger, http_post=posts, http_get=Scripted(APPROVED_GET),
    )
    assert response["result"]["isError"] is False
    assert '"overall": "success"' in response["result"]["content"][0]["text"]


async def test_dispatch_surfaces_activation_error_as_a_tool_error(ledger):
    response = await handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "marketing_activate_campaign", "arguments": {"campaign_id": ""}}},
        token=TOKEN, ledger=ledger, http_post=Scripted(), http_get=Scripted(),
    )
    assert response["result"]["isError"] is True
    assert "campaign_id is required" in response["result"]["content"][0]["text"]


# --- the real httpx wiring, via httpx's own MockTransport (not a monkeypatch
# of httpx itself — this is httpx's first-class, documented test double) -----

async def test_http_post_and_get_call_real_httpx(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"posted": True})
        return httpx.Response(200, json={"got": True})

    monkeypatch.setattr(http_handler, "_TRANSPORT", httpx.MockTransport(handler))
    try:
        posted = await http_handler._http_post("https://backend.test/x", json={"a": 1})
        got = await http_handler._http_get("https://backend.test/x")
    finally:
        monkeypatch.setattr(http_handler, "_TRANSPORT", None)
    assert posted.json() == {"posted": True}
    assert got.json() == {"got": True}
