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
make pitwall                    # the people finder   → http://localhost:8501
make rocketapply                # the job application app → http://localhost:8502
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

- **No phone numbers, ever.** Hunter returns them; PitWall does not request,
  store or display one. There is no column for it and no code path to it.
- **No SMTP verification.** No `RCPT TO`, no port-25 connections, no code that
  could. This is an engineering finding as much as a policy one — residential
  ISPs block port 25, Microsoft 365 answers `250` for addresses that do not
  exist, and catch-all domains accept everything. Even done perfectly it yields
  a graded guess. See `src/pitwall/enrich/checks.py`, which sets out all four
  reasons at the top.
- **No crawling for addresses.** Reading a page the search index already
  pointed at is in scope; walking a domain harvesting `mailto:` links is not.
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
or annotated is exempt. Delete & blocklist is permanent, prevents re-ingestion,
and **hard-deletes any email address held for that person**. Addresses have a
second, stricter clock of their own: past the window they go even when the
person stays, unless you actually acted on them.

---

## Finding an email address

Added in v3, and off the critical path: nothing below runs during a search, and
nothing runs over the whole table. You tick people on the **👤 People** page and
press one button.

**PitWall's own free logic runs first, and usually finishes the job.** There is
no contact database to buy here — a local SearXNG index is a serviceable
substitute at 25 people a month:

```
1. domain        the company's mail domain, from your CSV, its careers page,
                 search results already stored, or one search — never a credit
2. harvest       any addresses already published at that domain
3. pattern       the format those addresses imply — first.last, flast, …
4. corroborate   search for the exact address. Found published anywhere public?
                 That is stronger evidence than an SMTP probe would be.
```

Every finding carries a status **and the evidence behind it**, always together:

| | earned by |
|---|---|
| 🟢 **Confirmed** | the exact address is published somewhere public, or Hunter says it is deliverable |
| 🟡 **Likely** | the company's format, agreed on by ≥2 real addresses, and the domain accepts mail |
| ⚪ **Guess** | one example, or the format most companies use. A hypothesis |
| ⚫ **Nothing found** | free search came up empty. Hunter may know, for a credit |

`pattern first.last (n=3)` means three known addresses at that company agree.
**n=1 and n=5 never render the same**, and there is no confidence percentage
anywhere — a number invites you to trust a hypothesis.

The expensive part is learning a company's format, and that answer is reusable:
resolve Alpine once and every later Alpine candidate is free. If the domain is
wrong — a car dealership rather than the race team, which really happens —
correct it in the person's panel and it outranks every automatic source for good.

**Fill the domains in once and that problem mostly disappears.**
`companies.domain` is checked before any guessing:

```bash
make domains          # propose one per company, with the evidence. Writes nothing
make domains-apply    # …once you have read the table
make domains-show     # which are filled in
```

It probes which search engines are currently answering *and* honouring quoted
phrases before it starts — they suspend each other's CAPTCHAs by the hour, and
one that silently ignores quotes returns the open web. When none passes, it
refuses to run rather than guess. Anything it gets wrong is one edit in
`config/companies.csv`.

**Hunter.io is optional, manual, and one credit at a time.** Free plan: 50
credits a month, no card. It is never called by a search, never in bulk, and
never without you clicking on one specific person. Live credits sit in the
sidebar; at zero the buttons disable with the reset date rather than failing on
click. A paid domain lookup feeds its results back into the free engine, so it
permanently improves it.

Addresses are **excluded from every export unless you tick the box**, and the
evidence string travels with them when you do.

```bash
pitwall enrich -c 41 -c 88          # free engine only; cannot spend
pitwall enrich -c 41 --github       # also read public commit emails
pitwall enrich -c 41 --hunter       # allow the paid escalation
pitwall export --include-contacts   # opt in, deliberately
```

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
make pitwall
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

`make pitwall` (`streamlit run apps/pitwall/Home.py`). The job side is moving to its own
app, RocketApply (`make rocketapply`, `apps/rocketapply/`); until it covers everything, the
job pages below stay in PitWall too.

| page | what it is for |
|---|---|
| **🏁 Home** | headline metrics, the last run, follow-ups due, top unreviewed leads |
| **⚙️ Setup** | edit both CSVs in a grid, validation report, provider/quota status, backup & reset |
| **🔎 Run Search** | pick companies/roles/templates, **see the cost before you commit**, run in the background with live progress, and **resume the AI analysis** for anyone a stopped run or a triage budget never read |
| **👤 Leads** | the table you live in: filter, sort, select a person, read the evidence, act |
| **📋 Pipeline** | kanban by CRM status, with overdue follow-ups pinned in red |
| **📊 Analytics** | coverage heatmap, score distribution, funnels, quota burn-down |
| **🎯 Job Search** | the daily job inbox — new matches, filtered-out jobs with their reasons, save / dismiss / mark applied |
| **📨 Applications** | the application tracker: status, dates, next actions, history |
| **🧭 Job Setup** | your job-search profile, skills, target companies, and source health |

On the Leads page, selecting a row shows **why this score**: the heuristic reasons,
the LLM reasons, the red flags, and every search result that surfaced the person
with its raw title and snippet. When a score looks wrong you can see exactly why.

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

# finding an email address — never automatic, never over the whole table
pitwall enrich -c 41 -c 88            # free engine only: it cannot spend
pitwall enrich -c 41 --github         # also read public commit emails
pitwall enrich -c 41 --hunter         # allow the paid escalation, 1 credit
pitwall enrich -c 41 --dry-run        # run it, write nothing

# where things stand, and what to export
pitwall stats
pitwall coverage                      # how much of the company list is actually searched
pitwall export --format md --min-score 55
pitwall export --include-contacts     # addresses are excluded unless you ask

# tailored CVs and cover letters
make fonts                            # vendor the three typefaces (once)
make validate-profile                 # is profile/ sound?
make calibrate                        # does the CV frame still measure as it claims?
pitwall apply preview quantitative    # a CV for a role family — no job, no model
pitwall apply jobs                    # the best-scoring postings, with ids
pitwall apply analyze --job 930       # matched / partial / missing, one model call
pitwall apply research Anthropic      # what they build, from their own pages
pitwall apply research X --dry-run    # what the model would see; calls no model
pitwall apply research --show         # what is cached, and what was rejected
pitwall apply generate --job 930 --dry-run     # the plan, writing nothing
pitwall apply generate --job 930               # CV + letter + provenance
pitwall apply generate --job 930 --no-llm      # deterministic CV only
pitwall apply generate --job 930 --record      # also track it as SHORTLISTED
pitwall apply tidy                    # one folder per job + output/README.md
pitwall apply tidy --dry-run          # what would move, moving nothing
pitwall apply voice-eval --job 930 --job 2922  # generate, read, score 1-5
pitwall apply voice-eval --show                # the trend across prompt versions

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
