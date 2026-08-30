# 🏁 PitWall

**A networking lead finder.** Give it a list of companies and a list of roles; it
gives you back a scored, filterable, note-taking pipeline of real people you can
reach out to — and, since v2, the live job openings at those companies with the
people you already know there attached to each one.

Job boards show you *openings*. They do not show you *people* — the engineers,
analysts and heads of department who actually decide, who post about their work,
and who answer a good message. PitWall finds those people, ranks them, and keeps
track of who you already looked at.

Motorsport is the default profile and the one it was built for; `PITWALL_PROFILE`
points it at any other field. Pure Python: Streamlit, SQLite, free search, free
LLMs. Runs locally, costs $0.

---

## Quickstart

```bash
make venv                       # .venv + dependencies (Python 3.11+)
.venv/bin/pitwall init          # config, .env, providers, validation
make searxng                    # local search that supports operators (needs Docker)
.venv/bin/pitwall validate      # check your two CSVs
.venv/bin/pitwall run --max-queries 50
.venv/bin/streamlit run app/Home.py
```

That is the whole thing. Everything below explains a piece of it.

> **Always use `.venv/bin/python`.** On macOS the system `python3` is often
> Anaconda's, whose `pydantic-settings` is too old to import this package. `pitwall
> init` checks for that and says so by name.

## What it costs: nothing

Every provider below has a free tier that needs **no credit card** and does not
convert to paid. If one changes, the chain degrades to the next rather than
breaking.

| | what it does | sign-up |
|---|---|---|
| **SearXNG** | self-hosted search, and the only provider that supports `site:` and quoted phrases | `make searxng` — no account |
| **ddgs** | keyless fallback search, no operators | nothing to do |
| **Serper** | 2,500 one-time credits, held in reserve for bursts | [serper.dev](https://serper.dev) |
| **Groq** | the LLM scoring, ~1 s per batch | [console.groq.com](https://console.groq.com) |
| **Z.ai / DeepSeek** | permanent free tiers, optional fallbacks | [z.ai](https://z.ai) · [deepseek.com](https://platform.deepseek.com) |
| **Ollama** | local, offline last resort; minutes per batch on CPU | [ollama.com](https://ollama.com) |

Google's Custom Search JSON API is **not** an option: it is closed to new
customers and shuts down on 1 January 2027.

## What it does not do

This list is deliberate, not a roadmap. None of it is coming.

- **No email or phone discovery.** No harvesting, no pattern inference, no SMTP
  verification, not even a column for it. A LinkedIn profile URL is the contact
  surface.
- **No LinkedIn fetching.** No page requests, no login, no cookies, no headless
  browser. Identity comes only from the title, URL and snippet a search engine
  already publishes.
- **No sending.** Drafting a message is fair game; sending one is not, and there
  is no code that could.
- **No scraping around a refusal.** `robots.txt` is enforced in code, and a site
  that declines an honest User-Agent is left alone — `motorsportjobs.com` is
  configured and disabled for exactly that reason.
- **No multi-tenant hosting, no accounts.** One person, one machine, one SQLite
  file.

Retention is 180 days for people you never touched; anyone you saved, contacted
or annotated is exempt. Delete & blocklist is permanent and prevents
re-ingestion.

---

## What it does

```
companies.csv + roles.csv
        │
        ▼  1. query builder      company × role × template, prioritized, budget-capped
        ▼  2. search             searxng → ddgs → serper, concurrent, cached 7 days
        ▼  3. extract & dedupe   SERP rows → distinct people, keyed on the LinkedIn slug
        ▼  4. heuristics         free, deterministic, explainable — kills most of the noise
        ▼  5. LLM filter         batched JSON verdicts with reasons and red flags
        ▼  6. job openings       live postings × the people you already have there
        ▼  7. dashboard          filter · ⭐ save · ❌ ignore · 📝 note · 📩 contacted · 🔄 follow-up
```

Every stage writes to SQLite and is independently resumable. If a run dies at
stage 5, stages 1–4 do not repeat.

**The hard part is not finding links — it is deciding which links are real.** A
query for `McLaren "Data Scientist"` returns McLaren Racing engineers, McLaren
Automotive engineers, McLaren Health Care analysts in Michigan, and people who
left in 2019. Two scoring stages exist entirely to beat that problem, and every
score is traceable back to the search-result snippet that produced it.

---

## Install

Requires Python 3.11+.

```bash
make venv                     # or: python3.11 -m venv .venv && pip install -r requirements.txt
.venv/bin/pitwall init        # writes config/*.csv and .env, checks your setup
```

`pitwall init` is safe to run twice — it finishes an interrupted setup rather
than resetting one.

## First run

```bash
# 1. Your two CSVs. `pitwall init` writes commented samples; this fills them from
#    the bundled motorsport research tables instead.
.venv/bin/python scripts/seed.py                        # config/seed/motorsport_*.csv
.venv/bin/python scripts/seed.py --source plan --force  # or the plan's §17 starter grid

# 2. Create the database
.venv/bin/pitwall init-db

# 3. Search that supports operators. Without it, 8 of the 12 query templates
#    never fire and coverage suffers badly.
make searxng && make searxng-check

# 4. The LLM. Free, and ~1 s per batch instead of a minute.
#    Put GROQ_API_KEY in .env, then:
make groq-check
#    …or stay local and offline:
.venv/bin/python scripts/setup_ollama.py

# 5. See the plan, and the cost, without touching the network
.venv/bin/pitwall build-queries --dry-run
.venv/bin/pitwall preview

# 6. Run it
.venv/bin/pitwall run --max-queries 50 --max-results 15

# 7. Live job openings, cross-referenced against the people you just found
.venv/bin/python scripts/discover_ats.py    # once
.venv/bin/pitwall jobs
.venv/bin/pitwall openings

# 8. Look at your leads
.venv/bin/streamlit run app/Home.py
```

Skip step 4 entirely and everything still works — you get heuristic scores
without the LLM judgement layer.

---

## The two files you edit

Everything follows from `config/companies.csv` and `config/roles.csv`. You can
edit them in a text editor or in the app's **⚙️ Setup** page.

<!-- BEGIN generated: csv-columns -->
<!-- generated by `pitwall columns`; edit src/pitwall/inputs.py, not this block -->

#### `config/companies.csv`

| column | type | rule | why it matters |
|---|---|---|---|
| `name` | text | required, unique (case-insensitive); a duplicate row is skipped | The canonical spelling. Everything keys on it. |
| `aliases` | pipe-separated | warned about when empty | How the company is written in the wild — season names, sponsor names, the short form people type. The single biggest lever on RECALL. |
| `negative_keywords` | pipe-separated | warned about when empty | Other organisations that share the name. The single biggest lever on PRECISION: 'McLaren Health Care' is a hospital network. |
| `linkedin_slug` | text | optional | Unused by search; kept for your own reference. |
| `domain` | text | optional | Used to guess the ATS board in `discover_ats.py`. |
| `series` | text | optional | Free text, shown in the UI. |
| `country` | text | optional | A candidate located here scores slightly higher. |
| `tier` | 1-3 | must be 1, 2 or 3; anything else defaults to 2 | Search priority. Tier 1 is searched first and most. |
| `active` | true/false | defaults to true | false keeps the row without searching it. |

#### `config/roles.csv`

| column | type | rule | why it matters |
|---|---|---|---|
| `title` | text | required, unique (case-insensitive); a duplicate row is skipped | The canonical spelling of the job. |
| `aliases` | pipe-separated | warned about when empty | What the same job is called elsewhere. |
| `family` | enum | required, and must be one of the active profile's role families | Groups roles for the coverage matrix and the heuristic layer. `pitwall validate` prints the legal values. |
| `seniority` | pipe-separated | each value must be in the profile's seniority ladder | Empty means any level. |
| `keywords` | pipe-separated | optional | Words that, in a snippet, suggest this really is the job — each is worth a few points in the heuristic layer. |
| `priority` | 1-3 | must be 1, 2 or 3; anything else defaults to 2 | Search priority within a company. |
| `active` | true/false | defaults to true | false keeps the row without searching it. |

With the `motorsport` profile, `family` must be one of:

> `data_science`, `data_engineering`, `strategy`, `performance`, `simulation`, `vehicle_dynamics`, `software`, `operations`, `leadership`

and each `seniority` value one of:

> `intern`, `graduate`, `junior`, `mid`, `senior`, `lead`, `head`, `executive`, `unknown`

Pipe-separated columns use `|` as the separator: `McLaren F1|McLaren Applied|MTC`.
<!-- END generated: csv-columns -->

Both files are validated on load. Errors and warnings appear as a table on the
Setup page — never as a stack trace.

**Season names change every year.** That volatility belongs in `aliases`, which
is why it is a CSV column and not code.

---

## The dashboard

`streamlit run app/Home.py`

| page | what it is for |
|---|---|
| **🏁 Home** | headline metrics, the last run, follow-ups due, top unreviewed leads |
| **⚙️ Setup** | edit both CSVs in a grid, validation report, provider/quota status, backup & reset |
| **🔎 Run Search** | pick companies/roles/templates, **see the cost before you commit**, run in the background with live progress |
| **👤 Leads** | the table you live in: filter, sort, select a person, read the evidence, act |
| **📋 Pipeline** | kanban by CRM status, with overdue follow-ups pinned in red |
| **📊 Analytics** | coverage heatmap, score distribution, funnels, quota burn-down |

On the Leads page, selecting a row shows **why this score**: the heuristic reasons,
the LLM reasons, the red flags, and every search result that surfaced the person
with its raw title and snippet. When a score looks wrong you can see exactly why.

---

## Command reference

`pitwall` is installed by `pip install -e .`; `python scripts/run_pipeline.py` is
the same program for anyone who has not.

```bash
# first run, and checking your inputs
pitwall init                    # config, .env, providers, validation report
pitwall validate                # what is wrong with your CSVs, and why
pitwall columns                 # the CSV contract, generated from the rules

# planning (no network)
pitwall build-queries --dry-run
pitwall preview

# stages, individually — each is resumable
pitwall search  --limit 50 --concurrency 4
pitwall extract --reprocess
pitwall score   --heuristic-only
pitwall score   --triage-min 25 --triage-top 200   # spend the LLM where it counts

# a full run, narrowed
pitwall run --companies "AF Corse,PREMA Racing" \
            --roles "Data Engineer,Race Strategy Engineer"

# re-score after a prompt change, without searching again
pitwall score --rescore-all --prompt-version v2

# iterate on the parser entirely offline
pitwall search --cache-only && pitwall extract --reprocess

# live job openings, and who you know at those companies
python scripts/discover_ats.py        # once; finds public ATS boards
pitwall jobs --score-fit              # poll sources, score fit vs config/profile.md
pitwall openings                      # the join, in the terminal

# where things stand, and what to export
pitwall stats
pitwall coverage                      # how much of the company list is actually searched
pitwall export --format md --min-score 55

# housekeeping
make searxng-check                    # is the search container healthy?
make groq-check                       # which models can this key reach?
python scripts/purge_stale.py --apply --vacuum
python scripts/eval_scoring.py relabel # §1.3: sweep + a labelling worksheet
python scripts/eval_scoring.py sweep
python scripts/sanitise_eval_set.py --check   # no real people in tracked fixtures
```

---


## Scoring, in two stages

**Stage 1 — heuristics.** Free, deterministic, instant, and explainable. Runs on
100% of candidates. Company name in the title is worth +30, an alias +24, a
snippet-only mention +14; the role ladder works the same way. A hit on that
company's `negative_keywords` is −40, a past-tense marker (`ex-`, `former`,
`until 2023`) is −20, a recruiter marker is −18. Everything below the threshold
is rejected without spending a single LLM token.

**Stage 2 — the LLM.** The survivors go to an open-weight model in batches of 8,
at `temperature=0`, with a JSON schema. It answers: is this a *current* employee,
does the company actually match (`exact` / `parent` / `subsidiary` /
`same_name_different_org` / `none`), does the role match, what seniority, score
0–100, and 1–3 reasons quoting the evidence.

`same_name_different_org` is the value that matters most: it is what separates
McLaren Racing from McLaren Health Care, Williams Racing from Williams Companies,
Alpine F1 from Alpine the audio brand — programmatically, with a reason attached.

**Final score:** `0.30 · heuristic + 0.70 · llm`, then multiplied by 0.35 if they
are not a current employee, 0.10 for `same_name_different_org`, 0.40 for an
unrelated role, and 0.80 if the model was unsure. Bands: ≥75 strong · 55–74
possible · 35–54 weak · <35 reject.

If the model returns something unparseable, the candidate is marked
`needs_review` and shown to you. **Nobody is ever silently dropped because the
model misbehaved.**

### Tuning with evidence, not vibes

`tests/fixtures/eval_set.csv` holds 60 hand-labelled candidates from a real run.

```bash
python scripts/eval_scoring.py sweep       # threshold vs precision / recall / F1
python scripts/eval_scoring.py evaluate    # heuristics vs LLM vs both
```

That is how the shipped `heuristic_threshold` of **12** was chosen rather than the
plan's suggested 25: on the free provider, quoted-phrase operators are
unavailable, so titles carry less signal and 25 costs too much recall.

```
threshold   precision   recall    F1     kept
    25         0.67      0.62    0.65     30      ← below the 0.90 recall floor
    12         0.61      0.94    0.74     49      ← chosen
     5         0.59      1.00    0.74     54
```

Re-run the sweep every time you change the prompt, the rubric or a threshold.

---

## Configuration

Three layers, in increasing order of volatility. Precedence:
**CLI flags > `.env` > `config/settings.yaml` > code defaults.**

- **`config/settings.yaml`** — thresholds, budgets, weights, bands. Committed.
- **`.env`** — API keys and machine-specific choices. **Never committed.**
- **CLI flags / UI controls** — per-run overrides.

Config is validated on import, so a typo fails loudly at startup instead of
silently mid-run.

### Search providers

| provider | free tier | operators | notes |
|---|---|---|---|
| `ddgs` | unlimited, keyless, rate-limited | partial | **default.** 1 query / 4 s, backed by a 7-day cache |
| `serper` | 2,500 trial credits | full Google | set `SERPER_API_KEY`; credits tracked and shown in the app |
| `searxng` | unlimited, self-hosted | full | the permanent escape hatch from rate limits |

Set the chain with `SEARCH_PROVIDERS=ddgs,serper`. Providers without a key are
skipped automatically; a provider that runs out of quota disables itself for the
rest of the run and the chain falls through to the next one.

Templates that need Google operators are **dropped automatically** on providers
that do not support them, and the heuristic layer does the exact-phrase matching
client-side instead.

### LLM providers

| provider | model | cost | notes |
|---|---|---|---|
| `ollama` | `pitwall-qwen2.5` | free, local, no key | **default.** Run `python scripts/setup_ollama.py` once. |
| `zai` | `glm-4.7-flash` | free (~1,000 req/day) | needs `ZAI_API_KEY`; far faster than local CPU |
| `deepseek` | `deepseek-v4-flash` | ~$0.14 / 1M in | the paid upgrade; batching keeps the cached-prompt discount |

All three speak the OpenAI-compatible Chat Completions API, so only `base_url`,
`api_key` and `model` change. If a cloud quota runs out mid-run, the pipeline
degrades to Ollama when it is reachable, and otherwise saves progress and exits
cleanly. **It never loses work.**

Without any LLM at all the pipeline still runs — heuristics only, which is
already useful.

#### Local Qwen setup

```bash
python scripts/setup_ollama.py        # pull, configure, and prove it on a real batch
python scripts/setup_ollama.py --check # verify only, change nothing
```

This pulls `qwen2.5:7b` and derives `pitwall-qwen2.5` from it with `num_ctx` raised
to 8,192. Both details matter:

- **Qwen2.5, not Qwen3.** Qwen3 is a hybrid reasoning model with thinking on by
  default, and Ollama has [no reliable way to turn it off](https://github.com/ollama/ollama/issues/14617).
  For a temperature-0 JSON classifier those tokens are pure cost — minutes per
  batch on CPU, and a real risk of pushing the JSON past the token budget.
- **The derived model exists for `num_ctx`.** A batch of 8 needs ~3,600 tokens of
  context; Ollama's default is 4,096 and it truncates **from the front**, which
  would silently delete the system prompt — the entire scoring rubric — and score
  every candidate against nothing. Ollama's `/v1` endpoint ignores per-request
  `options`, so the only place to fix this is the model itself. Deriving costs no
  extra disk: it reuses the base model's content-addressed layers.

PitWall refuses to run rather than risk this: `preflight()` checks the model exists
and has a large enough window, and fails with the exact remedy if not. If you
change `llm.batch_size`, re-run the setup script — the requirement scales with it.

**Speed.** Local inference on a CPU-only machine is minutes per batch. Expect
roughly 6–11 minutes per batch of 8 on an Intel laptop — fine for a few hundred
candidates overnight, slow for a full sweep. `LLM_PROVIDER=zai` with a free key is
the faster path if you have one; scoring is resumable either way.

---

## Legal and ethical guardrails

These constraints are architectural, not optional. Read §1.3 of the plan.

1. **Search engines only, never the platform.** PitWall queries a search API and
   reads what the search engine already publishes: title, URL, snippet. It never
   fetches `linkedin.com/in/...` pages, never logs in, never uses a session
   cookie, never drives a headless browser.
2. **Minimize.** Only what a networking decision needs: name, public headline,
   employer, profile URL, location, and your own notes. No emails, no phone
   numbers, no photos.
3. **Retention.** `python scripts/purge_stale.py --apply` deletes candidates that
   have been ignored or untouched for 180 days. People you saved, contacted or
   wrote a note about are never purged.
4. **Right to object.** **🚫 Delete & blocklist** removes a person and prevents
   re-ingestion on every future run. That is both your "not interested" button
   and your GDPR erasure mechanism.
5. **Never publish the database.** `data/` and `.env` are gitignored from commit #1.
   `tests/fixtures/` *is* tracked, because the labelled eval set is what makes a
   threshold change measurable — and as of v2 every identity in it is synthetic.
   The eval set and both SERP captures were sanitised by
   `scripts/sanitise_eval_set.py`; the real labelled set now lives in
   `data/eval_set_real.csv`, which is git-ignored, and `eval_scoring.py` prefers
   it when it is there. `python scripts/sanitise_eval_set.py --check` is the
   guard, and so is `tests/test_eval_set_privacy.py`.
6. **Rate-limit politely, and take no for an answer.** One query per second
   through SearXNG, exponential backoff on 429s, a shared token bucket across
   worker threads, and a seven-day cache so the same query is never issued
   twice in a week. For job sources, `robots.txt` is parsed and applied in code
   — and a site that refuses an honest User-Agent is left alone rather than
   worked around.

There is **no outreach-sending capability by design**, and there will not be one.
Motorsport is a small industry; automated messaging burns your reputation in it.

---

## Development

```bash
pytest                                    # 260 tests, no network required
pytest --cov=pitwall --cov-report=term    # ~90% coverage
ruff check . && mypy                      # lint and types
```

Tests are hermetic: they use a throwaway project root with their own `config/`
and never touch your real database, CSVs or cache. The saved SERP fixtures in
`tests/fixtures/` are real captures — they cost quota once and serve forever.

### Layout

```
config/          companies.csv · roles.csv · settings.yaml · query_templates.yaml
  profiles/      motorsport.yaml (the default) · generic.yaml
  seed/          the motorsport research tables seed.py reads
  job_sources.yaml · profile.md (your CV, for job-fit scoring)
docker/searxng/  compose file + instance settings for the search container
src/pitwall/     config · profiles · models · db · inputs · query_builder · ratelimit
                 pipeline · crm · export · cli · setup_wizard
  search/        base · cache · ddg · serper · searxng
  extract/       linkedin (the SERP parser) · normalize · dedupe
  scoring/       heuristics · llm · jobs · schema · prompts/
  sources/jobs/  base · ats · aggregator · career_page
app/             Home.py + pages/ (Setup · Run Search · Leads · Pipeline · Analytics · Openings)
scripts/         run_pipeline · seed · purge_stale · eval_scoring · setup_ollama
                 setup_groq · check_searxng · discover_ats · sanitise_eval_set
tests/           unit + integration + the labelled eval set (synthetic identities)
data/            pitwall.db · cache/ · exports/ · backups/ · eval_set_real.csv  (gitignored)
```

---

## Troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| Every query returns 0 results | the upstream engines are captcha-ing SearXNG, or DuckDuckGo rate-limited you | `make searxng-check` — it names the blocked engines. Lower `searxng_queries_per_second`, or wait an hour |
| `site:` queries return nothing useful | SearXNG is not running, so `ddgs` is leading the chain and operator templates are dropped | `make searxng`; `pitwall build-queries --dry-run` shows which provider and how many templates |
| The LLM stage fell back to Ollama | Groq's per-minute token ceiling, misread as exhaustion | Fixed in v2 — a per-minute limit is waited out, a per-day one degrades. Check the run notes |
| A company returns nothing at all | wrong or missing alias | Check the Analytics coverage heatmap — empty rows are flagged for you |
| Wrong-industry people scoring high | empty `negative_keywords` for that company | Fill the column, then `score --rescore-all` |
| `current_company` comes out empty | unusual SERP title format | Add the string to `tests/test_linkedin_parser.py` and extend the regex |
| Lots of `needs_review` verdicts | model returning bad JSON | Shorten `llm.batch_size` to 4 (then re-run `setup_ollama.py`), or try a cloud provider |
| `LLM unavailable: ... has no num_ctx set` | the model would silently truncate the prompt | `python scripts/setup_ollama.py` — this is the guard working, not a bug |
| LLM calls time out | `llm.timeout_seconds` too low for local inference | Raise it in `config/settings.yaml`; local batches take minutes, not seconds |
| Recall looks poor | threshold too high for your provider | `python scripts/eval_scoring.py sweep` |
| Stale data after an edit in the app | cache not cleared | Every write calls `invalidate()` then `st.rerun()` — file a bug if you see it |

Backups: the database is a single file. `cp data/pitwall.db data/backups/` — or
press **Backup database** on the Setup page. Your CRM notes are the irreplaceable
part; the candidates can always be re-found.
