# 🚀 RocketApply

Find the jobs worth applying to, rank them against your CV, generate a tailored
CV and cover letter for each, track every application, and find who to write to
at the companies you applied to.

RocketApply used to live inside the PitWall repo (`networking_lead_finder`). It
now has its own folder and repo; PitWall stays the motorsport networking /
outreach app.

## Quickstart

```bash
make venv           # .venv, requirements, and an editable install
cp .env.example .env   # then add GROQ_API_KEY (and any other keys)
make test           # the whole suite, no network
make rocketapply    # the app, on http://localhost:8502
```

## Where the data lives

- **Database:** `DB_PATH` in `.env`; on Simon's Mac `data/rocketapply.db`, its
  own file since 15 Oct 2026 (a full copy of PitWall's `pitwall.db` taken that
  day, so it still holds the people tables too — harmless, and the "who to
  write to" panel uses them).
- **Generated applications:** `output/` (a link to the PitWall folder's `output/`
  on Simon's Mac until the split is finished).
- **Your profile:** `profile/` (tracked), plus `config/job_search.yaml`,
  `config/my_skills.yaml` and `config/my_profile.md` (local, gitignored).
- Nothing in `data/`, `output/` or `.env` is ever committed.

## What is shared with PitWall

The engine is still the `pitwall` Python package (`src/pitwall/`): the database,
settings, job sources, search, email lookup and run tracking. RocketApply's
"who to write to" panel runs that people search. This repo carries its own copy,
so the two apps run and change independently, but a fix to that shared code has
to be made in both repos.

## RocketApply

`make rocketapply` → http://localhost:8502. The job side as one process, left to
right. The navigation *is* the process: each page carries how many jobs wait
there, and a job's status anywhere in the app is the name of the page it is on:

**Search · 10,907 → Selected · 8 → Generate · 44/49 → Review · 41 → Applications**
(statuses: Selected → In review → In process → Applied)

*Not interested* (Search, Selected, Review) dismisses the vacancy, not one list
row: it leaves every stage that has not gone out yet, and a later search does
not bring it back from any source. Selecting it again (Filters → Include
dismissed) is the undo.

Generation runs in a process of its own (`apps/rocketapply/rocket/worker.py`),
so closing the tab, restarting the server or a host that stops idle servers
does not stop it; the Generate page follows its files in
`run/rocketapply/<run>/`. Start the app from a terminal with `make rocketapply`
for long runs, not as a Claude preview: the Claude app stops a preview server
after its session has been idle 30 minutes.

| screen | what it is for |
|---|---|
| **Search** | a search box over every collected job (answers in ~70 ms; no job board is called), a Filters popover with a count beside every option, removable filter chips, a table you tick, *Select the top N*, and a detail panel: the 8-part score, for / watch-out, skills, other sources, the posting. *Fetch new jobs* runs a collection with a live per-source table. |
| **Selected** | CV only, or CV + letter (+ an independent fact-check under Advanced); **today's Groq budget** — how many fit, from the key ledger over the rolling 24 h; the best-scored ticked up to that cap; a hint per letter; Generate. |
| **Generate** | every job's eight-stage track, and what each stage produced as it produced it: the posting's skills with their quotes, coverage, the claims the letter may not make, the chosen projects and why, the letter plan, **each draft as the model writes it**, the checks, the CV. A run outlives the server; if the worker itself stops, *Resume* makes only what is left. |
| **Review** | the letter, editable (saved edits go through the same gates, the model's text is kept), CV and letter previews, the review note; Approve / Regenerate (fresh, not cached) / Not interested; **Export N approved → CSV**. |
| **Applications** | *In process* by export batch with one **Mark N as applied**; *Applied* with the People panel — LinkedIn searches you click, PitWall's people search for that employer (recruiters, hiring managers, peers, alumni; kept out of the motorsport lead list in `data/pitwall_employers.db`), free email finding, Hunter only on a confirmed click, a ready note (≤200 characters, with the request) and message (once they accept) per person from `config/outreach_templates.yaml`, or a note drafted by AI on a click (never sent). Tick several applications for one run and one **Outreach CSV**: a row per person, however many roles you applied to at that company, with a `check` column for anyone to look at before writing; *Outcomes*; *Exports*. |
| **How it works** | the sources and their keys, run history, the score's parts and weights, what each hard rule removed, calibration against your ratings, the stages with the model at each, the gates, the key pool, quality numbers including your own letter ratings. |
| **Settings** | the search profile, CV text for the AI, skills, target companies (the same files as PitWall's Job Setup), who to contact, and a check of `profile/`. |

The export CSV (`output/exports/<batch>.csv`) has one row per application:
company, role, location, score, why it matched, concerns, **the link to apply
at, and the absolute path of the CV PDF, letter PDF, letter text and CV DOCX**
where they already are in `output/`. `apply tidy` never moves a folder a CSV
points at. Letters are typeset on the CV's letterhead (`pitwall apply letters`
does it for runs made before that existed); a letter marked DO_NOT_SEND never is.

A CV costs ~2.6k Groq tokens (one call reads the posting; the rest is
deterministic); a CV + letter ~18-21k; with the fact-check ~34-61k. Five free keys
give ~1M a rolling day.

---

## The job search

The second half of the app, built to `job_search_assistant_engineering_spec.md`:
a personal job search for entry-level ML / data / AI roles that collects,
filters, ranks and explains, then tracks the applications. It shares the
database with the people search and leaves the people search alone —
its employers live in their own list.

```
sources ─► normalise ─► dedupe ─► hard filters ─► score + reasons ─► inbox ─► tracker
```

| where | what |
|---|---|
| `config/job_search.yaml` | your profile: countries, roles, experience limit, work authorisation, weights. **Personal, gitignored**; `job_search.example.yaml` loads until it exists. Edit it on 🧭 Job Setup. |
| `config/my_skills.yaml` | your skills, proficiency and evidence. Personal, gitignored. |
| `config/skills_vocab.yaml` | the skill vocabulary both sides are mapped onto (`sklearn` = `scikit-learn`). |
| `config/target_companies.csv` | employers to watch, priority A/B/C, and their public ATS board |

**Sources.** Thirteen market-wide sources, plus company boards, all free.
Every source is switched on and off on 🧭 Job Setup:

| kind | sources |
|---|---|
| aggregators | Adzuna (DE NL FR GB CH IT ES US SG), Jooble (US), Arbeitnow (DE) and Arbeitnow UK, Workable's cross-company search (every country), Findwork |
| official portals | EURES (every EU country and CH), JobTech/Platsbanken (SE), MyCareersFuture (SG), ITJobs.pt (PT) |
| country boards | GermanTechJobs, SwissDevJobs, DevITJobs UK and NL |
| remote boards | Himalayas (searched for where *you* can work from), Jobicy, Remote OK |
| company boards | every target company's ATS board (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Personio, Teamtailor), plus boards **found automatically** from the links inside other sources' postings, verified to belong to that employer |

Keys go in `.env` (see `.env.example`): `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`,
`JOOBLE_API_KEY_US`, `ITJOBS_API_KEY`, `FINDWORK_API_KEY`. A missing key skips
that source and says so. Keyless sources obey robots.txt (RFC 9309, wildcards
included). A keyed API is used under its own terms, since several put a
blanket `Disallow: /` on the API host. Each keyed source has a quota ledger:
Adzuna's 250 a day / 1,000 a week / 2,500 a month are counted over rolling
windows, spent quickly until everything open has been read once, then evenly.

Searches are built from your role labels and preferred titles
(`jobsearch/queries.py`), never kept by hand per source. Each search keeps a
cursor: the next visit asks only for what is newer. Searches that keep finding
relevant jobs come round more often.

**Grading.** Eight parts: role, experience, skills, location, visa, direction,
company, CV match. A skill named only as "a plus" counts a third. The UK and NL
sponsor registers (refreshed weekly) and USCIS H-1B data (download the CSV
from the USCIS H-1B Employer Data Hub into `data/sponsors/`) name employers
that sponsor. Agency postings are marked. `make job-calibrate` measures how
well each part agrees with your 👍 / 👎, dismissals and applications, and
proposes weights.

No LinkedIn, no scraping behind logins, no browser impersonation.

**Rules the whole thing keeps.** An unknown never rejects a job — only what a
posting *states* can: "3+ years required", "we cannot sponsor", "PhD required".
Rejected jobs stay visible under *Filtered out* with their reason. Every score
comes with its `+` reasons, `?` uncertainties and `-` concerns. Visa status is
always quoted from the posting, never inferred and presented as fact. The same
vacancy from several sources is one card, and the employer's own board is the
Apply link.

```bash
make job-search        # poll, filter, rank — prints the new matches
make job-rerank        # after editing your profile: re-rank, no network
make job-calibrate     # how well the score agrees with your ratings
make job-targets-ats   # find (and verify) ATS boards for target companies
```

It runs every morning at 07:00 from `scripts/launchd/com.pitwall.jobsearch.plist`
(installed 28 Sep 2026; `launchctl bootout gui/$(id -u)/com.pitwall.jobsearch`
stops it).

---

## Tailored CVs and cover letters

Built to `CV_COVER_LETTER_ENGINEERING_SPEC.md`. Ninety per cent deterministic,
ten per cent model:

> **The model decides what is *relevant*. The program guarantees what is *true*.**

```bash
make fonts
python3 profile/validate_profile.py
python scripts/run_pipeline.py apply calibrate
python scripts/run_pipeline.py apply preview quantitative
python scripts/run_pipeline.py apply jobs
python scripts/run_pipeline.py apply analyze --job 930
python scripts/run_pipeline.py apply generate --job 930
```

`make fonts` vendors DM Sans, Arimo and Archivo Black into `assets/fonts`.
`calibrate` checks the frame still measures as it claims. `preview` renders a CV
with no job and no model at all. `analyze` is a gap report for one model call.

### The CV is a frozen frame

The layout never changes: `CONTACT → SKILLS → EXPERIENCE → PROJECTS → EDUCATION`,
all three jobs, 3/3/1 bullets, the education block verbatim. Exactly three things
vary — which 3 of the 22 projects appear, what fills each skills row, and which
pre-approved variant a bullet uses. The model has no field with which to change
anything else, so the blast radius of a model error is three slots.

**Fitting is a guarantee, not a hope.** Every piece of text declares its cost in
rendered lines, measured by asking ReportLab's own paragraph engine rather than
dividing a character count. Enumerating all 1,140 legal project trios gives 47
body lines and one page every time.

`apply calibrate` re-derives the geometry and fails on drift. It reports the
margin in the direction that matters: how much narrower the column can get before
a bullet gains a line and the page breaks.

### The truth layer

`forbidden_claims` is the profile's do-not-claim list, plus every skill at
`confidence: none`, plus **every requirement this posting asks for that the
profile cannot evidence** — because the most dangerous hallucination is the one
that closes the gap the matcher just noticed.

Nothing is forbidden without checking the profile first. Both the analyzer and
the job search propose gaps, and the job search's inventory is narrower: it calls
`simulation` missing while the profile carries Monte Carlo path simulation in
three places. A proposed gap the profile disproves is reported and **not**
silenced. Neither are activities — only named technologies are worth forbidding,
because gagging "model development" suppresses true work while gagging "AWS"
prevents a false claim.

Beyond the spec, the layer enforces the rules `profile/` carries in its own right:

| Rule | What it does |
|---|---|
| `never_mention` | Work authorisation, visas, residency, nationality are a **topic** block — never raised, even when a posting asks. Simon answers that himself. |
| `not_my_work` | Three attributions belonging to teammates, blocked by `block_terms` because matching a whole sentence blocks nothing. |
| `never_claim_specifically` | RL in the trading work — a **co-occurrence** rule, so the sepsis project's real RL evidence stays sayable. |
| `evidence_private` | Claimable in prose; no public project exists to cite, so none may be named. |
| `conflicts_with` | A project whose work already prints in the experience block is never selected. |
| `readme_state` | A stub or absent README drops the GitHub label. The bullets stand alone; the link is what hurts. |
| `gated_verbs` | Matched across inflections — `owns` and `managing`, not just `owned`. The bare stem needs a subject, so "my own harness" passes. |

### Nothing unverified reaches `output/`

The deterministic gates always block: a number with no profile record, a
forbidden entity, a gated verb, a blocked topic. A rejected draft goes to `run/`
marked `REJECTED` and the command exits non-zero.

**The model audit does not block, and that is deliberate.** `fact_check` routes to
local Qwen first so the auditor is never the model that wrote the text. But
neither available judge is reliable enough for a veto: Groq is the writer, and
Qwen2.5-7B is independent but imprecise — on a real run it marked *"built a
seven-stage resumable pipeline"* unsupported while that phrase sat verbatim in
the facts it was given.

So the audit gets what it is good for. Its objections drive a rewrite, up to
three rounds; anything still flagged is written as
`..._Cover_Letter_NEEDS_REVIEW.txt` with the sentences printed. A human reading
three highlighted sentences is the outcome the design wants. A 7B model silently
discarding a good letter is not.

An unreadable verdict is recorded as **unchecked**, never as a pass — Qwen
answered one real call with `{"isAllowed": false, ...}` instead of the schema's
keys, and defaulting a missing `verdict` to SUPPORTED would have turned the
safety net into a rubber stamp.

Every application ships a `provenance.json` naming the profile ids it used, the
commit they came from, and every model call. That is what makes a line on the CV
answerable six months later.

### Making the letters sound like Simon

The first version wrote competent, anonymous prose. It followed every rule and
avoided every banned phrase, because it had far more *instructions about* the
voice than *examples of* it: 22 banned phrases, 6 abstract voice notes, and about
310 words of real writing. It had never seen a finished letter in that voice, so
shape, pacing and the closing paragraph had nothing to copy from.

`profile/style.yaml` therefore carries `exemplars` — whole letters, each marked
`verdict: good` or `verdict: avoid` — and they lead the writer's prompt, ahead of
every rule. Three are loaded, about 1,000 words: the Alpine letter as the anchor,
and the Waymo and Red Bull letters as what to avoid. The `avoid` ones earn their
place by being *the same person writing less well*, which isolates what went
wrong rather than which words to dodge. `validate-profile` warns while either
verdict is missing.

**Showing a model bad prose has a cost**, and it is handled. A model shown any
text absorbs its nouns, and the Red Bull letter names "Snowit" as an employer —
which appears nowhere in `profile/`. So every capitalised word in the exemplars
that the profile cannot account for is collected into `truth.contaminants` and
blocked for the run, minus whatever this posting's own employer and role
legitimately supply. The same letter says "I co-founded BeeSurfing" where
`experience.yaml` records a University team project, so `co-founded` and
`founded` are now gated verbs.

**The studio is the better place to do this.** `make pitwall` →
**✍️ Applications Studio**: pick a posting, read the advert without leaving the
page, generate, then read the CV and the letter side by side with the evidence
behind them, and score it there.

Everything generated stays listed under **Everything generated so far**, graded
or not, so a letter can be read now and scored tomorrow. That list is rebuilt
from each run's `provenance.json` rather than held in memory, so it survives
switching pages, closing the tab and restarting the server — losing a letter by
navigating away to look at the job advert was the original complaint. A grade
attaches to one *generation*, not to the job, because the same posting at the
same prompt version gets generated more than once and each attempt deserves its
own verdict. Rating a
letter means reading four paragraphs of prose, and a terminal prompt is a poor
place for that. Both front-ends call the same
`pitwall.apply.pipeline.generate_application`, so a letter made in the browser
and one made in the terminal are the same letter.

`voice-eval` keeps that honest. Each round scores every letter on naturalness
(1–5) and edit burden (%), keyed by prompt version, so a change that helps one
letter and quietly hurts the next shows up instead of being argued about. The
scores are Simon's; nothing in the loop grades itself. Ship at an edit burden
consistently at or under 30%.

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

### Contact enrichment providers

| provider | free tier | needed? | notes |
|---|---|---|---|
| PitWall's own engine | unlimited | — | domain, harvest, pattern, corroboration. No key of any kind. Needs SearXNG for the operator queries |
| `hunter` | 50 credits/month, no card | optional | `HUNTER_API_KEY`. Manual, per-person, one credit at a time |
| `github` | 60 req/hour, 5,000 with a token | optional | `GITHUB_TOKEN`, no scopes needed. Public commit emails |
| DNS (MX) | unlimited | optional | `dnspython`. Port 53 is never blocked, which is why this works where SMTP would not |

Everything except the first is optional and everything degrades: no Hunter key
means no paid button, no `dnspython` means the MX check reports "unknown", and
no SearXNG means the harvest and corroboration queries say so rather than
returning noise. Hunter's costs, verified against its own documentation on
1 September 2026: Email Finder 1 credit (charged only when it finds something),
Domain Search 1 credit per 10 addresses, Email Verifier 0.5, Account free.

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
   employer, profile URL, location, your own notes, and — since v3, and only
   when you ask for it person by person — a work email address. No phone
   numbers, no photos, no address you did not press a button to find.
3. **Public sources only, for addresses too.** An address here was either found
   already published on a public page, worked out from the format of addresses
   that were, or returned by Hunter under your own account. Nothing is probed,
   and no domain is crawled.
4. **Retention.** `python scripts/purge_stale.py --apply` deletes candidates that
   have been ignored or untouched for 180 days. People you saved, contacted or
   wrote a note about are never purged. Email addresses have a second, stricter
   clock: past the window they go even when the person stays, unless you acted
   on that person.
5. **Right to object.** **🚫 Delete & blocklist** removes a person, hard-deletes
   every address held for them, and prevents re-ingestion on every future run.
   That is both your "not interested" button and your GDPR erasure mechanism.
   Enrichment refuses to run on a blocklisted person at all.
6. **Nothing leaves by accident.** Exports exclude every address unless you pass
   `--include-contacts` or tick the box, and when they do go out the evidence
   goes with them, so a guess cannot be read as a fact in a spreadsheet.
7. **Never publish the database.** `data/` and `.env` are gitignored from commit #1.
   `tests/fixtures/` *is* tracked, because the labelled eval set is what makes a
   threshold change measurable — and as of v2 every identity in it is synthetic.
   The eval set and both SERP captures were sanitised by
   `scripts/sanitise_eval_set.py`; the real labelled set now lives in
   `data/eval_set_real.csv`, which is git-ignored, and `eval_scoring.py` prefers
   it when it is there. `python scripts/sanitise_eval_set.py --check` is the
   guard, and so is `tests/test_eval_set_privacy.py`.
8. **Rate-limit politely, and take no for an answer.** One query per second
   through SearXNG, exponential backoff on 429s, a shared token bucket across
   worker threads, and a seven-day cache so the same query is never issued
   twice in a week. For job sources and the one page an address harvest may
   read, `robots.txt` is parsed and applied in code — and a site that refuses
   an honest User-Agent is left alone rather than worked around.

There is **no outreach-sending capability by design**, and there will not be one.
Motorsport is a small industry; automated messaging burns your reputation in it.

---

## Development

```bash
pytest                                    # 1,169 tests, no network required
pytest --cov=pitwall --cov-report=term    # ~90% coverage
ruff check . && mypy                      # lint and types
```

Tests are hermetic: they use a throwaway project root with their own `config/`
and never touch your real database, CSVs or cache. The saved SERP fixtures in
`tests/fixtures/` are real captures — they cost quota once and serve forever.

"No network" is **enforced, not assumed**: an autouse fixture in
`tests/conftest.py` blocks every non-loopback socket, so a test that forgets to
inject a stub fails loudly instead of quietly reaching Hunter, GitHub, DNS or
somebody's running SearXNG. `tests/test_enrich_policy.py` goes further and
asserts the standing prohibitions against the whole tree: no SMTP import, no
`RCPT TO` / `MAIL FROM` / port 25 in any line of code, no phone column in the
schema, no crawling primitives in `enrich/`.

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
  enrich/        base · domains · harvest · patterns · corroborate · checks
                 hunter · github · cache   (finding an email address)
apps/pitwall/     Home.py + pages/ — the people finder (dark theme in .streamlit/ beside it)
apps/rocketapply/ app.py + views/ + ui/ — the job application app (light theme beside it)
scripts/         run_pipeline · seed · purge_stale · eval_scoring · setup_ollama
                 setup_groq · check_searxng · discover_ats · sanitise_eval_set
                 resolve_domains (fills companies.domain from search)
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
| An email lookup says "guess (n=0)" with no samples | SearXNG is not running, so the harvest and corroboration queries were refused rather than run unquoted | `make searxng`; the notes on the result say which step stopped and why |
| Every email lookup resolves the wrong company | `companies.domain` is empty, so the domain is being guessed from stored search results — which include news sites and retail arms | `make domains`, read the table, `make domains-apply`. Or set it per company in the person's panel |
| `make domains` says no engine is usable | you have run enough queries to CAPTCHA every upstream engine | Wait — `SearxEngineTooManyRequests` clears in 15 min and a CAPTCHA in an hour (`docker/searxng/settings.yml`). Lower `--qps` next time |
| Recall looks poor | threshold too high for your provider | `python scripts/eval_scoring.py sweep` |
| Stale data after an edit in the app | cache not cleared | Every write calls `invalidate()` then `st.rerun()` — file a bug if you see it |

Backups: the database is a single file. `cp data/pitwall.db data/backups/` — or
press **Backup database** on the Setup page. Your CRM notes are the irreplaceable
part; the candidates can always be re-found.
