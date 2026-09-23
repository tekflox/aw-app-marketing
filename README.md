# aw-app-marketing

Paid-social campaigns for aw-workspace: take a shortlist of products, turn it
into a campaign an agent creates on Meta **paused**, and hand a human the Ads
Manager link. Activation still always needs a human — either they press play
themselves in Ads Manager, or they ask for it and a real approval request goes
to them on Telegram through this app's own `marketing_activate_campaign`.

The whole design is one sentence: **this app is the deterministic middle, and it
holds neither credential.**

| | Who does it | Why not here |
|---|---|---|
| read the product catalog | the app that owns the shop, with the shop's credential | duplicating a store credential into a public repo, and welding a generic app to one shop |
| create campaigns / ad sets / ads | **Meta's own hosted MCP server** (`https://mcp.facebook.com/ads`) | Meta ships it; a wrapper would be a worse reimplementation that falls behind the Graph API every release |
| score products, plan, build creative, keep an audit trail | **this app** | these have hard rules, and a model re-deriving them each run is neither reproducible nor reviewable |

The two credentials meet in exactly one place: an **agent's tool belt**. Never
in a process, never in this repo.

## What you get when it's installed

* **Two gateway upstreams**, both written into one `mcp.json` by this app:
  * `marketing` — the six tools below, on `/api/apps/marketing/mcp`
  * `meta-ads` — Meta's hosted Ads MCP, with your access token
* **One agent** — `marketing-sonnet`, plus its agent config
* **One skill** — `aw-marketing`, the flow contract the agent loads
* **An audit ledger** at `<AW_WORKSPACE_HOME>/data/marketing/campaigns.jsonl`

### Tools

| Tool | Does |
|---|---|
| `marketing_filter_products(products, hint)` | scores a catalog you fetched against a collection hint, returning a ranked shortlist **with the matched terms behind each score** |
| `marketing_campaign_plan(brief, products, …)` | the full campaign/ad-set/ad spec, `PAUSED` throughout, plus a human-readable summary. Refuses to invent a budget or a country |
| `marketing_build_creative(products, …)` | a carousel `object_story_spec` validated against Meta's limits offline. Refuses when a product has no image |
| `marketing_record_campaign(plan, meta_ids)` | appends what Meta created and returns the Ads Manager link. Idempotent on `campaign_id` |
| `marketing_activate_campaign(campaign_id)` | the ONLY sanctioned way to flip a recorded campaign to `ACTIVE`. Sends a real human approval request (naming the campaign, account and budget it already recorded) and blocks until it's approved; fails closed on denial, timeout or an unreachable backend |
| `marketing_list_campaigns(limit)` | what was launched, when, for which products |

Five of the six are pure and offline; `marketing_activate_campaign` is the one
exception, and its entire job is that one gated network call. **None creates a
campaign. None reads a catalog.** Two tests pin exactly that
(`tests/test_routes_and_mcp.py`).

## Configuration

Nothing here ships with a value. The Meta access token is a credential and
goes through its own route, straight to the workspace's encrypted secret
store; everything else is a plain per-install setting:

```bash
curl -X POST http://127.0.0.1:9030/api/apps/marketing/settings \
  -H "X-Api-Key: $AW_WORKSPACE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"meta_access_token": "..."}'

curl -X POST http://127.0.0.1:9030/api/apps/marketing/config \
  -H "X-Api-Key: $AW_WORKSPACE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"meta_ad_account_id": "act_...", "meta_page_id": "...",
       "meta_instagram_actor_id": "...", "default_daily_budget_minor": 500,
       "default_country": "PT", "default_currency": "EUR"}'
```

`POST /settings` (and `POST /logout` to clear it) is the only way the token
ever moves — `POST /config` never sees it. Posting it to `/config` would be a
mistake, not a shortcut: that endpoint has no notion of the schema's
`x-secret` marker and would happily write it into `loaded.config`, which is
plain and cloud-synced.

The rest of the config lands in the workspace's `config_store`
(`<AW_WORKSPACE_HOME>/app-config/marketing.json`) — outside the package dir, so
it survives an app update, an uninstall/reinstall and a workspace redeploy. The
repo versions only the schema.

Check what's missing at any time:

```bash
curl -s -H "X-Api-Key: $AW_WORKSPACE_API_KEY" \
  http://127.0.0.1:9030/api/apps/marketing/status | jq
```

Until `meta_access_token` is set, the `meta-ads` upstream is registered
**disabled** rather than enabled-and-401ing. An upstream that connects and
serves zero tools reads as a broken app; a disabled one reads as an
unconfigured app, which is the truth.

### The token is in this app's OWN secret store, not `aw-secrets`

Two different mechanisms, easy to conflate:

* **`aw-secrets`** is the shared, human-gated vault — every read pings a
  person on Telegram. Wrong fit here: the gateway needs this token on every
  activation with nobody in the loop.
* **`ctx.secrets`** (this app, `secrets:own`) is the workspace-local encrypted
  store (`src/apps/secret_store.py`, Fernet at rest) every app with a bearer
  credential uses for exactly this — same pattern as aw-app-notion's
  `notion_token`, aw-app-git's `github_token`, aw-app-android-studio's
  `remote_token`. No human in the loop, and never plain config.

The token is in the second one, not the first — and not in plain config
either, which is the gap this section used to leave open.

### Prerequisites nobody can code around

1. Meta app with the MCP server enabled and the `ads_mcp_management` permission,
   and an access token generated from it. **Own-account use needs no App Review**
   (Advanced Access + review is for agencies). User tokens expire in ~60 days;
   when `meta-ads` starts returning 401, that's what happened.
2. A Business Manager ad account **with a payment method**, plus the Page id and
   Instagram business account id. Without the ad account, creation fails even
   for a paused campaign.

## Two things not to "clean up"

**`status: PAUSED` on everything `marketing_campaign_plan` builds.** It is not
a cautious default, it is a scope boundary: the gateway's approval gate covers
agent-*run* tools only (`apps/mcp-gateway/back/gateway/config_gateway.py`), not
arbitrary upstream tool calls, so nothing between an agent and Meta's
ad-*creation* API asks a human first. Making the plan parametrically `ACTIVE`
would start spending before a human has even seen the campaign exist.
Re-budgeting and retargeting stay a person's job, in Ads Manager, always —
that part never changes.

Activation is no longer flatly refused, and that is a deliberate policy
change, not an erosion of this one: `marketing_activate_campaign` is a
separate, dedicated tool that puts a real human approval request in front of
every activation, built from what this app already recorded creating, never
from a free-form argument. See `marketing_app/activation.py`'s module
docstring for the full design and `skills/aw-marketing/SKILL.md`'s step (e)
for how an agent is meant to use it. What to resist "cleaning up" here is the
plan's own `status`, not the existence of activation altogether — don't make
`campaign_plan`'s `status` parametrizable; that would let the artefact a human
reviews also be the thing that spends money.

**This app writes its own `mcp.json`, and only its own code writes it** — on
activate, on a (non-token) config save, and from `POST /settings`/`POST
/logout` when the token itself changes. It does **not** ship
`mcp.template.json`, which is the workspace's usual answer for a credentialled
upstream. `mcp_template.render()` writes the file whole rather than merging,
and the runtime runs it *after* the plugin on both paths
(`src/apps/runtime.py:902` → `:930`; `src/apps/routes.py:763-765` → `:770`), so
shipping both would silently delete this app's own `marketing` entry on every
boot and every config save. `marketing_app/mcp/self_register.py` has the full
reasoning, and `tests/test_self_register.py` fails if a template ever appears.

`mcp.json` is generated and **gitignored** — it carries the workspace API key
and the Meta token, and this repo is public. It was in `.gitignore` before the
first `git add`, because the security scan reads git *history*, not just HEAD.

## Known gap (blocking a real v1)

Verified 2026-09-19 against a live store: the WordPress `tekflox-mcp` catalog
surface exposes `id, name, sku, brand, category, description, price,
formatted_price, url` and **no image field at all**. Every Meta ad needs media,
so `marketing_build_creative` refuses, correctly, and the flow stops at step (b′).

The fix is `image_url`/`images[]` on `get_catalog` in the **store's** plugin, not
here. The alternative worth considering first: if the store already feeds a
product catalog to Meta (WooCommerce→Meta feed), the images are already on
Meta's side and Advantage+ catalog ads beat hand-built carousels — less code and
a better result for a shoe shop.

Two more catalog findings the skill encodes: the store's `search` tool returns
`total_found: 0` for terms that demonstrably exist (so the skill forbids it and
uses paginated `get_catalog`), and descriptions carry expired promotions and
coupon codes verbatim (so creative copy is *extracted*, never copied).

## Agent scoping — read before trusting it

`agent-config-marketing` names a gateway profile restricting the agent to
`marketing`, `meta-ads` and the store's upstream. **Today that is documentation
of intent, not a security barrier.** This workspace's gateway ships
`configs: {}` (`apps/mcp-gateway/back/config/gateway.json`), so profile names
resolve to nothing and agents see the whole gateway. Treat the restriction as a
statement of what *should* be enforced once profiles work.

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt pytest jsonschema fastapi httpx
.venv/bin/python -m pytest tests/ -q          # 100% coverage gate, see pyproject.toml
.venv/bin/python tests/validate_manifest.py aw-app.json --schema <aw-marketplace>/schemas/aw-app.schema.json
```

Install it through the **marketplace**, not by pointing `package_dir` at this
checkout: the gateway's app-scan only reads `apps/<slug>/mcp.json`, so a
sideloaded app activates and serves zero MCP tools. After installing, re-check
`GET /status` and restart the `mcp-gateway` app — reinstalls have silently lost
config keys before, with every route still answering 200.
