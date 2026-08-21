# 🏁 PitWall

**A motorsport networking lead finder.** Give it a list of companies and a list of
roles; it gives you back a scored, filterable, note-taking pipeline of real people
you can reach out to.

Job boards show you *openings*. They do not show you *people* — the engineers,
analysts and heads of department who actually decide, who post about their work,
and who answer a good message. PitWall finds those people, ranks them, and keeps
track of who you already looked at.

Built to the specification in [`PITWALL_PLAN.md`](PITWALL_PLAN.md). Pure Python:
Streamlit, SQLite, free search APIs, open-weight LLMs. Runs locally, costs $0.

---

## What it does

```
companies.csv + roles.csv
        │
        ▼  1. query builder      company × role × template, prioritized, budget-capped
        ▼  2. search             ddgs (free) → serper (paid) → searxng, cached 7 days
        ▼  3. extract & dedupe   SERP rows → distinct people, keyed on the LinkedIn slug
        ▼  4. heuristics         free, deterministic, explainable — kills most of the noise
        ▼  5. LLM filter         batched JSON verdicts with reasons and red flags
        ▼  6. dashboard          filter · ⭐ save · ❌ ignore · 📝 note · 📩 contacted · 🔄 follow-up
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
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit if you have API keys
```

Nothing needs an API key to work. The default path — DuckDuckGo search, heuristic
scoring — is free, keyless, and runs offline against its cache after the first
sweep.

## First run

```bash
# 1. Write config/companies.csv and config/roles.csv
python scripts/seed_motorsport.py            # from motorsport_teams.csv / motorsport_positions.csv
python scripts/seed_motorsport.py --source plan --force   # or the plan's §17 starter grid

# 2. Create the database
python scripts/run_pipeline.py init-db

# 3. See the plan without touching the network
python scripts/run_pipeline.py build-queries --dry-run

# 4. Run it (about 4 s per query on the free provider — 50 queries ≈ 3½ minutes)
python scripts/run_pipeline.py run --max-queries 50 --max-results 15

# 5. Look at your leads
streamlit run app/Home.py
```

---

## The two files you edit

Everything follows from `config/companies.csv` and `config/roles.csv`. You can
edit them in a text editor or in the app's **⚙️ Setup** page.

### `config/companies.csv`

| column | example | why it matters |
|---|---|---|
| `name` | `Toyota Gazoo Racing` | canonical name, used in queries and matching |
| `aliases` | `TGR-E\|Toyota Gazoo Racing Europe` | pipe-separated; **massively** improves recall |
| `negative_keywords` | `dealership\|Toyota Motor Sales` | **the single highest-value column** — kills the wrong-org problem |
| `linkedin_slug` | `toyota-gazoo-racing` | optional; lets the parser hard-confirm an employer |
| `domain` | `toyotagazooracing.com` | optional; used by `site:` query variants |
| `series` | `WEC` | F1 / F2 / F3 / WEC / GT / IMSA / Supplier / Analytics |
| `country` | `Germany` | used by the location-match rule |
| `tier` | `1` | 1 = dream target, 3 = opportunistic. Drives the query budget. |
| `active` | `true` | switch a company off without deleting its history |

### `config/roles.csv`

| column | example | why it matters |
|---|---|---|
| `title` | `Race Strategy Engineer` | canonical role string |
| `aliases` | `Strategy Engineer\|Race Strategist` | pipe-separated |
| `family` | `strategy` | one of `data_science`, `data_engineering`, `strategy`, `performance`, `simulation`, `vehicle_dynamics`, `software`, `operations`, `leadership` |
| `seniority` | `graduate\|junior\|mid` | which levels you want for this role |
| `keywords` | `Monte Carlo\|undercut\|tyre degradation` | boosts the score when found in a snippet |
| `priority` | `1` | 1–3; drives the query budget |
| `active` | `true` | |

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

```bash
# planning (no network)
python scripts/run_pipeline.py build-queries --dry-run
python scripts/run_pipeline.py preview

# stages, individually — each is resumable
python scripts/run_pipeline.py search  --limit 50
python scripts/run_pipeline.py extract --reprocess
python scripts/run_pipeline.py score   --heuristic-only

# a full run, narrowed
python scripts/run_pipeline.py run --companies "AF Corse,PREMA Racing" \
                                   --roles "Data Engineer,Race Strategy Engineer"

# re-score after a prompt change, without searching again
python scripts/run_pipeline.py score --rescore-all --prompt-version v2

# iterate on the parser entirely offline
python scripts/run_pipeline.py search --cache-only && \
python scripts/run_pipeline.py extract --reprocess

# where things stand, and what to export
python scripts/run_pipeline.py stats
python scripts/run_pipeline.py export --format md --min-score 55

# housekeeping
python scripts/purge_stale.py --apply --vacuum
python scripts/eval_scoring.py sweep
python scripts/setup_ollama.py --check
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
   Note that `tests/fixtures/` deliberately *is* tracked, and the eval set and
   SERP captures inside it contain real people's names and public profile URLs —
   which is what makes them useful for tuning. **Keep this repository private.**
   If you ever need to push it somewhere public, delete `tests/fixtures/eval_set.csv`
   and `tests/fixtures/serp_*.json` first, or regenerate them with
   `python scripts/eval_scoring.py bootstrap` against synthetic rows.
6. **Rate-limit politely.** One query every four seconds, exponential backoff on
   429s, and a cache so the same query is never issued twice in a week.

There is **no outreach-sending capability by design**, and there will not be one.
Motorsport is a small industry; automated messaging burns your reputation in it.

---

## Development

```bash
pytest                                    # 220 tests, no network required
pytest --cov=pitwall --cov-report=term    # ~89% coverage
ruff check . && mypy                      # lint and types
```

Tests are hermetic: they use a throwaway project root with their own `config/`
and never touch your real database, CSVs or cache. The saved SERP fixtures in
`tests/fixtures/` are real captures — they cost quota once and serve forever.

### Layout

```
config/          companies.csv · roles.csv · settings.yaml · query_templates.yaml
src/pitwall/     config · models · db · inputs · query_builder · ratelimit · pipeline · crm · export
  search/        base · cache · ddg · serper · searxng
  extract/       linkedin (the SERP parser) · normalize · dedupe
  scoring/       heuristics · llm · schema · prompts/
app/             Home.py + pages/ (Setup · Run Search · Leads · Pipeline · Analytics)
scripts/         run_pipeline · seed_motorsport · purge_stale · eval_scoring · setup_ollama
tests/           unit + integration + the labelled eval set
data/            pitwall.db · cache/ · exports/ · backups/     (gitignored)
```

---

## Troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| Every query returns 0 results | DuckDuckGo rate-limited you | Wait 15 min, lower `ddgs_queries_per_second`, or add a Serper key |
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
