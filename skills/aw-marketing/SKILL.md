---
name: aw-marketing
description: Build a paid-social campaign (Instagram/Facebook) from a store's product catalog — pick the products for a collection, get the shortlist approved, draft plan and carousel creative, create the campaign PAUSED through Meta's own Ads MCP, and hand the Ads Manager link to a human. Use whenever the request is about creating, drafting or planning an ad campaign, promoting a collection or a set of products, or boosting something on Instagram/Facebook.
---

# aw-marketing — from catalog to a PAUSED campaign

You have three families of tools and they are not interchangeable:

| Tools | Who serves them | What they do |
|---|---|---|
| the **store's** catalog tools (`get_catalog`, `get_brands`, `get_top_products`, …) | the app that owns the shop, with the shop's own credential | read products |
| `marketing_*` | this app | score, plan, build a creative, record — all offline and deterministic, except `marketing_activate_campaign`, whose entire job is one gated network call |
| the **`meta-ads`** tools | Meta's own hosted MCP server | actually create campaigns, ad sets and ads |

This app creates nothing on Meta and reads no catalog. **You** are the bridge:
you hold both credentials, the app holds neither. Don't try to make a
`marketing_*` tool fetch products — pass them in.

## The flow

### (a) Find the products — with `get_catalog`, NEVER with `search`

**The store's `search` tool is broken. Do not use it.** Verified 2026-09-19
against the real catalog: it returns `"total_found": 0` for terms that
unambiguously exist — `botas` (a real category with products in it), `EXÉ` (a
brand with 88 products), `outono inverno`. It does not degrade gracefully or
error; it returns a well-formed empty result that looks like a valid answer.

If you use it you will conclude the shop has no matching products and report
that to the human. That report would be false.

Instead: **page through `get_catalog`** (`limit` caps at 100; ~500 products is
~6 calls) and filter client-side. Add `brand=` when the human named a brand —
that filter does work.

Two catalog quirks that will bite you:

* **A collection usually lives in the free-text `description`, not in the
  taxonomy.** A product's `description` opens with
  `"🌟 NOVA COLEÇÃO OUTONO-INVERNO 25/26 ✨ …"` while its `category` reads
  `"PROMO PV26, EXÉ, TÉNIS, NOVA COLEÇÃO EXÉ"`. So don't filter on `category`
  alone — that's exactly why `marketing_filter_products` scores both.
* **`brand` comes back EMPTY on an unfiltered listing.** `get_catalog(brand=…)`
  works and populates it; grouping by brand from a plain listing does not.
  `price` is also empty on a good fraction of products, and `category` is a
  comma-separated **string**, not a list.

### (a′) Score them — `marketing_filter_products(products, hint)`

Hand it everything you fetched plus the human's own words for the collection.
It returns a ranked shortlist with the matched terms behind each score, so the
human can see *why* a product is in. Accents, case and season-code spellings
(`PV 24` / `PV24`, `25/26` / `25-26`) are all handled.

Don't hand-pick the shortlist yourself instead. The point of the tool is that
the same catalog and the same hint give the same answer every time — which is
what makes the next step mean anything.

### (a″) Get the shortlist approved — mandatory, main path

Show the human the shortlist and **wait for an explicit yes.** This is not a
fallback for when you are unsure; it is the normal route, every time. Include
the near-misses the tool returns — "these three almost made it" is often how the
human notices you understood "autumn-winter" differently than they meant it.

### (b) Draft the plan — `marketing_campaign_plan(brief, products, …)`

Produces the full campaign / ad set / ad specification with `status: PAUSED`
throughout, plus a `human_summary` block. It **refuses** rather than guessing
when the daily budget or the target country is neither passed nor configured —
if you see that refusal, ask the human, don't pick a number.

Show them the `human_summary` and get approval before spending anything.

### (b′) Build the creative — `marketing_build_creative(products, …)`

Returns a validated carousel `object_story_spec`. It enforces Meta's limits
offline — 2–10 cards, and media, link and headline on every card — so you find
out here rather than from an opaque Graph API error later.

**It will refuse when a product has no `image_url`, and that refusal is
currently expected.** The store's MCP catalog surface exposes
`id, name, sku, brand, category, description, price, formatted_price, url` and
**no image field at all**. Without media there is no ad of any kind, not just no
carousel. If you hit this: say so plainly and stop. Do not scrape the product
page, do not guess a CDN path, do not substitute a stock image. The fix is in
the store's own WordPress plugin (expose `image_url` on `get_catalog`), which is
someone else's repo and someone else's decision.

Card copy is **extracted** from the description, not copied. Catalog
descriptions carry expired campaigns and coupon codes verbatim (`"PROMOÇÃO
Válida de 09/06/2023 a 30/10/2025"`, `"Use o cupão MIMINHOCRISPAL"`). Publishing
those is an ads-policy problem, not just a sloppy one. Never paste a raw
description into ad copy yourself either.

### (c) Create it on Meta — `meta-ads` tools, always `status: PAUSED`

Campaign, then ad set, then creative, then ad. **Every one of them PAUSED.**

`PAUSED` is not caution, it is the only brake that exists. The gateway's
approval gate covers agent-*run* tools only, not arbitrary upstream tool calls —
so nothing between you and Meta's ad API asks a human first. Creating something
`ACTIVE` starts spending money with no confirmation anywhere.

If a `meta-ads` call returns 401: the Meta access token has expired (user tokens
last ~60 days). Say exactly that and ask for a new one — don't retry.

If the `meta-ads` tools are missing entirely, the token isn't configured. Check
`GET /api/apps/marketing/status`, and tell the human which fields are empty.

### (d) Record it — `marketing_record_campaign(plan, meta_ids)`

Immediately after creation succeeds. It is idempotent on `campaign_id`, so it is
safe if you lost your place. Pass Meta's own permalink as
`meta_ids.permalink` when a creation tool returned one — it beats the link this
app derives.

### (e) Send the link — and activate only through the one gated tool

Give the human the Ads Manager link the previous step returned, and say the
campaign is paused:

```
https://business.facebook.com/adsmanager/manage/campaigns?act=<ad_account_id>&selected_campaign_ids=<campaign_id>
```

**Changing the budget and editing the targeting are always done by a person,
in Ads Manager. That has not changed.**

Activation used to be the same story — never you, not even if the human said
"go ahead" in the same message — because there was nothing between you and
Meta's ad-creation API except that refusal. That is no longer true: this app
now has `marketing_activate_campaign`, and it is the one sanctioned way to
make a campaign `ACTIVE`.

**Call it whenever activating comes up — an explicit "activate it" or an
ambiguous "go ahead, do it" folded into a longer message. You are not the
judge of how direct the request was, and you don't need to be.** The tool
itself decides nothing: it builds a real approval prompt from what
`marketing_record_campaign` actually recorded (the campaign's name, its ad
account, its daily budget — never anything you type) and sends it to a human
on Telegram. It blocks until they press Approve. Denied, ignored until it
expires, or the approval backend unreachable — all of that refuses, and
nothing activates. **The human's button press is what makes a request
"direct" now** — that is what replaced parsing your sentence for how sure you
sounded. So don't withhold the call out of caution either: offering to
activate by calling this tool is safe, because calling it is not the same as
activating.

Two things worth knowing before you call it, from how this played out for
real on a live account:

* It activates the whole tree — campaign, ad set, ad — and reports each one
  separately. **A partial result (the campaign goes `ACTIVE`, the ad set is
  rejected by Meta) is an expected outcome, not a bug.** Tell the human
  exactly what Meta said about the entity that failed; do not do the spend
  math yourself to second-guess it, and do not try to roll anything back —
  that is the human's call, same as the activation itself.
* If the result names `fallback_needed`, approval was already granted — the
  human already said yes. This app's own token just couldn't finish the
  Graph API call. Complete it with `ads_activate_entity` on the `meta-ads`
  upstream for the entities still not `ACTIVE`; do not go back and ask the
  human again, they already answered.

Never activate any other way. Never call a `meta-ads` tool directly to flip
`status` to `ACTIVE` — `marketing_activate_campaign` is the only path that
puts a human's real approval in front of it.

## What to do when you're blocked

Say what's missing and stop. In particular:

* **no `image_url` in the catalog** → report it as a blocker on the store's
  plugin. There is no v1 without it.
* **budget/country not configured and not given** → ask.
* **`search` returned 0** → you shouldn't have called it; use `get_catalog`.
* **`meta-ads` tools absent or 401** → token missing or expired.
* **`marketing_activate_campaign` comes back denied, expired or timed out** →
  say so plainly; the human did not approve it (or never saw the prompt in
  time). Don't retry silently, and don't reach for a `meta-ads` tool as a
  workaround — that is exactly the bypass the approval gate exists to stop.

None of these are things to work around. Working around them is how an agent
spends money on the wrong products with the wrong copy.
