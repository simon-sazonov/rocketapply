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
`site:linkedin.com/in "…"` best. If you point `SEARXNG_BASE_URL` at an instance you
do not control, clear `search.searxng_engines` so the adapter uses whatever that
instance considers its defaults — otherwise you are asking for engines it may not
have, and the adapter has to spend an extra request discovering that.

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
