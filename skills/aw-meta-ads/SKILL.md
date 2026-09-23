---
name: aw-meta-ads
description: Operating Meta's hosted Ads MCP (the `meta-ads` upstream, `ads_*` tools) for real — the call order that works, the minimum payloads for a campaign, ad set, video creative and ad, and the gotchas that cost a live ad account time. Covers Meta Ads API errors, video creative requirements (video_id + thumbnail + page_id), why ad set targeting gets silently rewritten or wiped, Advantage+ rewriting your copy, what to do when the access token expired or the ads tools are missing entirely, and where the ad account credentials, env vars and config values live. Load this before your first `ads_*` call.
---

# aw-meta-ads — operating Meta's Ads MCP without breaking something live

This is the playbook for the **`meta-ads` upstream**: Meta's own hosted MCP
server (`https://mcp.facebook.com/ads`), the thing that actually creates
campaigns, ad sets, creatives and ads. Its tools are the `ads_*` family.

It is **not** the `aw-marketing` skill. That one is the flow contract for this
app — catalog → shortlist → plan → creative → record → stop. Read that for
*what to do*; read this for *how the Meta side actually behaves*. If you are
here because something already went wrong, jump to §7.

Everything below was learned by operating a **real ad account with real
money in it**. The account identifiers are deliberately absent: write
`<AD_ACCOUNT_ID>` wherever you see it here and substitute the one from your
own workspace's config (§6).

---

## §0 — Everything is created PAUSED, and that is the only brake there is

Every campaign, ad set and ad you create carries `status: "PAUSED"`. Not as a
default you may override when the human sounds confident — as the single
safety mechanism in the entire path.

Here is the technical reason, and it is worth knowing precisely, because it is
not what you would assume:

> **Nothing between you and Meta's ad-creation API asks a human first.** The
> workspace gateway's approval gate covers agent-*run* tools — the ones that
> interrupt a person on Telegram before executing. It does **not** cover
> arbitrary upstream tool calls. `ads_create_campaign` is an arbitrary upstream
> tool call. So is `ads_activate_entity`. There is no prompt, no confirmation,
> no second pair of eyes anywhere in that path.

So `PAUSED` is not timidity and it is not a style preference. It is the place
where a human is inserted into a loop that otherwise has no human in it. An
`ACTIVE` campaign begins spending money the moment it is created, with nothing
having asked anyone.

Consequences you must hold to:

* **Never call `ads_activate_entity`.** Not on a campaign, not on an ad set,
  not on an ad. Not when the human says "go ahead" in the same message. If they
  want it live, they press the button in Ads Manager — that button *is* the
  safety model, and you removing it is not a favour.
* **Never change budget, targeting or status on an entity that is already
  live.** A live campaign has a human's decision behind it.
* This rule is enforced in three places in this app on purpose (the planner
  hardcodes `PAUSED`, its tests assert no `ACTIVE` appears anywhere in a plan,
  and the flow skill repeats it). Three layers, because the cost of the rule
  failing once is unbounded and the cost of it being redundant is zero.

---

## §1 — The call order that works

Four entities, in this order, each one referencing the last. Reversing any two
of them fails, because Meta resolves the parent at creation time.

```
ads_create_campaign   →  campaign_id
ads_create_ad_set     →  ad_set_id      (needs campaign_id)
ads_create_creative   →  creative_id    (needs page_id, media)
ads_create_ad         →  ad_id          (needs ad_set_id + creative_id)
```

Before any of that, one orientation call each — do not guess these ids:

| Call | Gives you |
|---|---|
| `ads_get_ad_accounts` | the ad accounts this token can actually reach |
| `ads_get_ad_account_pages` / `ads_get_user_pages` | the Page ids that may own a creative |
| `ads_get_ig_accounts` | the Instagram actor id, if the ad runs on Instagram |

The configured values in §6 are the intended ones; these calls tell you whether
your token can see them. When the configured account is not in
`ads_get_ad_accounts`, the token is wrong or under-scoped — that is a §7
problem, not something to route around by picking a different account.

### Minimum payloads

These are the shapes that worked, not a full field reference. Meta's own field
documentation is reachable in-band with `ads_get_field_context` and
`ads_get_help_article` — use those instead of guessing a field name.

**Campaign**

```json
{
  "act_id": "act_<AD_ACCOUNT_ID>",
  "name": "<campaign name>",
  "objective": "OUTCOME_TRAFFIC",
  "status": "PAUSED",
  "special_ad_categories": []
}
```

`special_ad_categories` is required even when empty — Meta rejects the call
without it. `objective` is an ODAX value (`OUTCOME_TRAFFIC`,
`OUTCOME_SALES`, `OUTCOME_ENGAGEMENT`, `OUTCOME_AWARENESS`, …); the older
`LINK_CLICKS`-era names are gone.

**Ad set**

```json
{
  "act_id": "act_<AD_ACCOUNT_ID>",
  "campaign_id": "<campaign_id>",
  "name": "<ad set name>",
  "status": "PAUSED",
  "daily_budget": 500,
  "billing_event": "IMPRESSIONS",
  "optimization_goal": "LINK_CLICKS",
  "targeting": {
    "geo_locations": {"countries": ["PT"]},
    "age_min": 18,
    "age_max": 65,
    "targeting_automation": {"advantage_audience": 0}
  }
}
```

* `daily_budget` is in **minor units** — `500` is €5.00, not €500. Getting this
  wrong is a 100× budget error in the direction that costs money.
* `targeting_automation.advantage_audience: 0` is not optional boilerplate.
  Leave it out and Meta may switch Advantage+ audience on for you; see §5 for
  what that then does to your targeting without telling you.
* The budget lives on the ad set here. If the campaign was created with
  campaign-budget optimisation, it lives on the campaign instead and Meta
  rejects an ad-set budget — pick one, and say which in the plan.

**Creative** and **Ad** — see §3, they have their own traps.

---

## §2 — Media: API upload is gated, the UI upload is not

**`ads_creative_upload_media` and `ads_creative_upload_local_image` can fail
with a "gradually rolled out … check back later" message**, and that is not a
transient error you should retry. It is Meta gating the *upload tools* for that
account. Retrying produces the same message indefinitely.

The important part, and the reason this is a workaround rather than a dead end:

> **The gate is on the API upload tools only, not on the account.** Media
> uploaded by a human through Ads Manager shows up perfectly well over the API
> and can be referenced in a creative you build programmatically.

So when upload is gated:

1. Tell the human the API upload is gated on this account, and ask them to
   upload the video (or image) in Ads Manager themselves. Be specific — they
   only need to get the file into the account's media library; they do not need
   to build anything.
2. Wait. Then call **`ads_get_ad_videos`** (or `ads_get_ad_images`) to find it.
3. Check `status.video_status` is `"ready"` and its processing phases are
   complete. A video that is still encoding will be accepted into a creative
   and then fail the ad.
4. Build the creative against that `video_id`, exactly as if you had uploaded
   it. Everything downstream works normally.

Do not scrape, do not re-host, do not substitute a stock asset, and do not try
to find another upload endpoint. The gate is Meta's decision about that
account; the human uploading the file is the sanctioned path around it.

### `ads_get_ad_videos` lies by omission

Called bare, it returns a **partial field set** — enough to look like a
complete answer, not enough to build a creative. In particular the
auto-generated thumbnail (`picture`) is missing, and you need that (§3).

Call it with explicit `video_ids` or an explicit `fields` list and the rest
appears. Same caution applies generally: when a Meta read tool returns an
object that seems to be missing something obvious, ask for the field by name
before concluding it does not exist.

---

## §3 — A video creative needs exactly three things

`ads_create_creative` for a video ad will not work without **all three**:

1. **`video_id`** — from the upload, or from `ads_get_ad_videos` per §2.
2. **A thumbnail** — `image_url` or `image_hash`. **Meta does not accept a
   video creative with no cover image.** This is the one people miss, because
   nothing about "I have a video" suggests you also need a still. The
   auto-generated `picture` from `ads_get_ad_videos` is a perfectly good
   `image_url` and is the cheapest way to satisfy this.
3. **`page_id`** — the Facebook Page the ad is published by. Required for any
   ad creative at all, video or not. It comes from config (§6); verify it with
   `ads_get_ad_account_pages`.

Plus, when the ad runs on Instagram: **`instagram_user_id`** (the IG actor).
And set **`call_to_action_type`** explicitly — see §5 for why leaving it to
default is a bad idea.

With those three present, plus the CTA and the copy, `ads_create_creative`
works on the first try. That is the whole gotcha: it is not finicky, it is
just unforgiving about the thumbnail.

### `ads_create_ad` takes a creative you already made

You do **not** have to rebuild the `object_story_spec` when creating the ad.
Reference the creative you just created:

```json
{
  "act_id": "act_<AD_ACCOUNT_ID>",
  "name": "<ad name>",
  "adset_id": "<ad_set_id>",
  "creative": {"creative_id": "<creative_id>"},
  "status": "PAUSED"
}
```

Rebuilding the spec inline is how you end up with two creatives that differ in
a field you did not notice, one of which is the one actually serving.

---

## §4 — `ads_update_entity` REPLACES `targeting`, it does not merge it

This is expected behaviour, documented here so you stop being surprised by it,
not a bug to report:

> **Sending a `targeting` object to `ads_update_entity` replaces the entire
> stored `targeting` object.** Any field you do not resend is gone — not
> defaulted, not preserved. `flexible_spec`, `genders`, `geo_locations`,
> interests: all of it.

So an "innocent" partial update that only touches the age range silently wipes
the interest targeting the human agreed to. The ad set still exists, still
looks fine in a list view, and is now targeting something else entirely.

The rule that follows:

* **Read the current `targeting` first** (`ads_get_ad_entities` on the ad set),
  **merge locally**, then send the whole merged object back.
* Never send a `targeting` fragment. There is no such thing as a fragment here.

The same property is genuinely useful when you mean it — deliberately dropping
a field (e.g. removing a gender lock) is done by simply not resending it. Just
make sure every omission is one you chose.

---

## §5 — Advantage+ rewrites your targeting and your copy behind your back

Three separate versions of the same problem: Meta's automation is on by
default, it edits things you already decided, and it does not announce it.

### Advantage+ audience silently rewrote a whole ad set

Observed, on a real ad set, after a human made an unrelated manual edit to the
location in Ads Manager. With `targeting_automation.advantage_audience` on,
Meta rewrote the targeting:

* **locked gender** to a single gender that nobody selected;
* **shrank the geography drastically** — most of the mainland regions
  disappeared, leaving a handful of outlying districts;
* **injected irrelevant interests**, including categories in an unrelated
  language and interests from an adjacent-but-wrong product category.

None of this raised an error. The ad set was simply targeting a different
audience than the one that was approved.

The fix, and the prophylactic:

```json
"targeting_automation": {"advantage_audience": 0}
```

Set it at creation (§1), and set it again as part of the full merged
`targeting` object (§4) whenever you correct an ad set. Then rewrite the
targeting explicitly — do not assume the corrupted values reverted.

**Re-read targeting after any human edit in the UI.** That is when this
happens. If a human tells you they "just changed the location", treat the whole
targeting object as suspect and check it.

### Advantage+ copy generation ships variants you did not write

The creative-text generation in the UI comes with **"Apply all" ticked by
default**, including AI-generated variants that can be entirely off-topic — a
variant about cars in an advert for shoes is the real example. Anything ticked
there will serve.

If a human is finishing the creative in the UI, tell them explicitly to review
and untick those before publishing. If you are building the creative over the
API, you control the copy and this does not apply — which is one more reason to
build it over the API.

### The default CTA is not the one you want

The Ads Manager UI defaults the call-to-action to **"See details"**, not "Shop
now". For a campaign whose whole purpose is to send people to a product page,
that default is a quiet downgrade.

Set `call_to_action_type` explicitly at creative creation (`SHOP_NOW`,
`LEARN_MORE`, …), or have the human change it in the UI. Do not let it default.

---

## §6 — Where every credential and config value lives

You (the agent) probably **cannot read any of these files** — the marketing
agent runs with `workspace_access: false`. This map is here so you can tell a
human *exactly* where to look, and so you can recognise which failure you are
looking at. The paths are stated in full for that reason.

**The first thing to call when anything smells like configuration:**

```
GET /api/apps/marketing/status
```

It answers `configured`, `meta_upstream_enabled`, and **names every field that
is empty**. This app reports its own degradation on purpose, because an
unconfigured app that answers 200 to everything is indistinguishable from a
working one. Read that response before theorising.

| What | Where it lives | Written by |
|---|---|---|
| `meta_access_token` — the Meta credential, ~60-day expiry, `ads_mcp_management` scope | the workspace's **encrypted secret store**, physically `<AW_WORKSPACE_HOME>/secrets/marketing.json` (`AW_WORKSPACE_HOME` is the workspace's `.aw-workspace/` tree) | `POST /api/apps/marketing/settings` — **never** the generic config route |
| `meta_ad_account_id`, `meta_page_id`, `meta_instagram_actor_id`, `default_daily_budget_minor`, `default_country`, `default_currency` | non-secret per-install config: `<AW_WORKSPACE_HOME>/app-config/marketing.json` | `POST /api/apps/marketing/config` |
| the audit ledger of what was launched | `<AW_WORKSPACE_HOME>/data/marketing/campaigns.jsonl` | this app's `marketing_record_campaign` |
| `AW_WORKSPACE_API_KEY` — for calling the workspace's own API | `os.environ` for in-process apps; `<AW_WORKSPACE_HOME>/.env` for standalone processes | the workspace, on every key generate/regenerate |

Four things worth knowing about that table:

* **The token is deliberately not in config.** Generic config is plain and
  cloud-synced and has no concept of a secret field, so a bearer credential
  landing there is a leak. It is also deliberately **not** in `aw-secrets` —
  that vault interrupts a human on Telegram for every read, and the gateway
  needs this token on every single activation with nobody in the loop. Same
  pattern as the Notion / Git / Android-Studio apps' tokens.
* **An empty token is not a crash.** The `meta-ads` upstream is registered
  DISABLED, so instead of 401ing with zero tools, every Meta step reports "not
  configured". That distinction is the whole point — see §7.
* **Nothing here invents a default.** No country, no budget, no account id. An
  unset value produces a refusal naming the missing field, because the
  alternative is money being spent somewhere nobody chose.
* **`AW_WORKSPACE_API_KEY` follows the template convention**, it is not a thing
  this app made up. Whoever has a filesystem should read
  `docs/app-workspace-api-auth.md` in this repo (vendored from
  `apps/aw-app-template/`) rather than reinventing the retrieval logic — it
  covers the in-process `os.environ` read, the `.env` fallback for external
  processes, and why you re-read it per call instead of caching it.

For a human debugging this: config is read back through `ctx.config`, the token
through `ctx.secrets`, and the reasoning for the split is written out in
`marketing_app/config.py`'s module docstring.

---

## §7 — When it fails

Say what is wrong and stop. Every entry below is a thing to report, not a thing
to work around — working around them is how an agent spends money on the wrong
audience with the wrong copy.

| Symptom | What it actually is | What to do |
|---|---|---|
| a `meta-ads` call returns **401** | the Meta access token expired. User tokens last ~60 days, and this is the ordinary end of their life, not an outage | Say exactly that, and ask for a fresh token with the `ads_mcp_management` scope. **Do not retry** — it will 401 again. It is pasted via `POST /api/apps/marketing/settings` (§6) |
| **no `ads_*` tools exist at all** | the token is not configured, so the upstream is registered DISABLED — or the gateway profile serving them is not being served | `GET /api/apps/marketing/status` and report which fields are empty. If status says configured and the tools are still absent, it is the gateway, not this app — say so rather than guessing |
| `ads_creative_upload_media` says "gradually rolled out … check back later" | the API upload tools are gated for this account | §2. Ask the human to upload in Ads Manager, then find it with `ads_get_ad_videos` |
| creative creation rejects a video | almost always the missing thumbnail | §3 — `image_url`/`image_hash` is mandatory alongside `video_id` |
| an ad set's targeting is not what was approved | either Advantage+ rewrote it (§5) or a partial `ads_update_entity` wiped it (§4) | Read the current targeting, show the human the diff, and rewrite the full object with `advantage_audience: 0` |
| a read tool returns an object that is missing an obvious field | partial field set by default | Re-call with explicit `fields` / ids (§2) |
| an error you do not recognise | — | `ads_get_errors` for recent account-level errors, `ads_get_field_context` / `ads_get_help_article` for what a field means. Report the actual message; do not paraphrase it into a guess |
| the campaign is created but the human wants it live | — | §0. They press the button. Always |

One meta-rule, because it is the one that generalises: **a Meta API failure is
usually a missing required field or an expired credential, not a reason to try
a different tool.** Read the error text, name the field, ask for what is
missing.
