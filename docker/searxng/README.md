# SearXNG — PitWall's primary search provider

`ddgs` reports `supports_operators() == False`, so the query builder drops every
template that needs `site:` or a quoted phrase — 8 of the 12 in
[`config/query_templates.yaml`](../../config/query_templates.yaml). That is why v1
only ever searched 7 of 49 companies.

A local SearXNG proxies Google, Bing, DuckDuckGo, Brave and Startpage through one
JSON API, passes operators through unchanged, needs no API key, and costs nothing.

## Start it

```bash
make searxng        # generates ./.env with a secret, then `docker compose up -d`
make searxng-check  # proves JSON works and an operator query returns profiles
```

Then put it first in the chain (this is already the default in
[`config/settings.yaml`](../../config/settings.yaml)):

```
SEARCH_PROVIDER_CHAIN=searxng,ddgs,serper
SEARXNG_BASE_URL=http://localhost:8888
```

## The three things that break

| Symptom | Cause | Fix |
|---|---|---|
| `403` with an HTML body | JSON output is off by default | `search.formats` must list `json` |
| Container exits immediately | `secret_key` is still the placeholder | `make searxng-env` writes `.env` |
| Every query returns 0 results | the bot limiter is throttling your own client | `server.limiter: false` |

All three are already set in [`settings.yml`](settings.yml). They are listed here
because they are the failures you will hit if you rebuild this from scratch.

## What "unlimited" does and does not mean

The container has no quota. The engines behind it do. Sweep Google hard enough and
it answers captchas, at which point SearXNG returns **HTTP 200 with an empty
`results` list** and an explanation in `unresponsive_engines`.

`src/pitwall/search/searxng.py` treats that case as a soft failure — it raises
`RateLimited`, tenacity backs off, and the chain falls through to `ddgs`. It is
never recorded as "zero results for this query", because a blocked engine and an
empty company look identical in the database otherwise.

Three defences, all on by default:

- a **1 query/second** token bucket (`search.searxng_queries_per_second`), shared
  process-wide by every worker thread;
- **engine rotation** — `search.searxng_engines` holds `;`-separated groups and each
  query uses the next one, so no single upstream engine absorbs the whole sweep;
- **backoff** on `unresponsive_engines`, as above.

## Engines

Upstream SearXNG ships `google` and `bing` **disabled**. [`settings.yml`](settings.yml)
turns them back on, because they are the two that handle
`site:linkedin.com/in "…"` best. The instance's own live list is at
`http://localhost:8888/config` — that, not this file, is the authority on which
engine names `search.searxng_engines` may use.

### What this actually looks like in practice

Measured against a fresh container on 29 Aug 2026, from a home connection:

- the operator query returned **30 results, 20 of them `linkedin.com/in` profile
  URLs** — the capability `ddgs` does not have, working;
- but within a handful of queries `google`, `duckduckgo`, `startpage`, `brave`,
  `qwant` and `mojeek` all answered with captchas or `access denied`, leaving
  `bing`.

That is normal and it is why the adapter is built the way it is. Expect a good
first pass, a thinner second one, and recovery after an hour. Two knobs matter
when it happens: lower `search.searxng_queries_per_second`, and check that
`search.suspended_times` in [`settings.yml`](settings.yml) is still overriding
upstream's 24-hour captcha ban with the one-hour value.

If you point `SEARXNG_BASE_URL` at an instance you do not control, clear
`search.searxng_engines` so the adapter uses whatever that instance considers its
defaults — otherwise every query pays an extra widening request.

## No Docker?

Do not attempt a bare-metal install — it is genuinely painful. Leave `ddgs` first in
`SEARCH_PROVIDER_CHAIN` and accept that operator templates stay dropped; everything
else in PitWall works unchanged, just with less coverage.

## Files

| File | What it is |
|---|---|
| `docker-compose.yml` | the service, host port 8888 → container 8080 |
| `settings.yml` | the instance config, every key checked against SearXNG's schema |
| `.env` | generated, git-ignored, holds `SEARXNG_SECRET` |
