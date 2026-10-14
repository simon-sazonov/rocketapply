# Scoring weights — what was done, and what is live

Written 29 Sep 2026 by the Claude session that built the job-search expansion (28–29 Sep).
It is for Simon, and for any chat working on RocketApply. Every number below was
measured on `data/pitwall.db`, not recalled.

---

## 1. The short answer: no tuned weights were applied

The seven original weights are unchanged. One new part was added on 28 Sep, at its default weight:

| part | weight | share of score | status |
|---|---|---|---|
| role | 25 | 23.8% | unchanged |
| experience | 20 | 19.0% | unchanged |
| skills | 20 | 19.0% | unchanged |
| location | 10 | 9.5% | unchanged |
| visa | 10 | 9.5% | unchanged |
| career (shown as "Direction") | 10 | 9.5% | unchanged |
| company | 5 | 4.8% | unchanged |
| **fit** (shown as "CV match") | **5** | **4.8%** | **new part, code default** |
| total | 105 | | |

`fit: 5.0` now appears in `config/job_search.yaml` because the profile was saved from the Job
Setup page on 29 Sep. Saving writes out every field, including defaults, so this is the default
value, not a tuned one.

Calibration proposed other weights ("fitted" and "blended", §5) six times. **Every proposal
was rejected** by the rule in §4, because none ranked Simon's own ratings better than the
current weights did. What changed the ranking instead is in §6.

---

## 2. Where the weights live, and who reads them

- **One file:** `config/job_search.yaml` → `weights:`. It is personal and gitignored. The model is
  `Weights` / `JobSearchConfig` in `src/pitwall/jobsearch/settings.py`.
- **One scorer:** `score()` in `src/pitwall/jobsearch/scoring.py`. It writes `job_matches`:
  `score`, `label`, `components` (each part's `value` **and** `weight`), and `config_hash`.
- **Both apps use that scorer.**
  - **PitWall** (`apps/pitwall/`) uses it on the Job Search and Job Setup pages.
  - **RocketApply** (`apps/rocketapply/`) does not score anything itself:
    - it reads `job_matches.score` (`rocket/runner.py` ~L342);
    - it runs searches and re-ranks through `pitwall.jobsearch.run.JobSearch`
      (`runner.start_search`, ~L369–384);
    - it shows the weights from `load_config()` (`views/engine.py` ~L120–134);
    - its "Measure the score against my ratings" button calls
      `pitwall.jobsearch.calibrate.calibrate()` (`views/engine.py` ~L143–146). **See §7.1.**
- **When a changed weight takes effect.** A weight changed in the YAML affects both apps only after
  a re-rank (`make job-rerank`, or RocketApply's re-rank). Until then, stored scores keep the old
  `config_hash`, and check 2 in §8 shows it.

---

## 3. The formula, exactly as coded

```
parts     p_i in [0, 1]          the 8 parts below
weights   w_i                    from config/job_search.yaml
base    = sum(w_i * p_i) / sum(w_i) * 100
score   = base
          - 8  * min(2, sum of share(level) over languages the posting requires)   LANGUAGE_PENALTY
          - 10 if the TITLE names an avoid_topics entry                            AVOID_PENALTY
          - 3  if posted by an agency / task marketplace                           AGENCY_PENALTY
score   = clamp(score, 0, 100)
label   = strong >= 75 · good >= 60 · fair >= 45 · weak below
share(level):  not listed / A1 / A2 = 1.0 · B1 = 0.5 · B2 / C1 / C2 / native = 0
```

Hard filters run first. A job that fails one is stored with `passed = 0` and shown under
"Filtered out", so **weights only rank jobs that pass**.

"Unknown" parts sit in the middle, so a posting that says little is neither lifted nor buried.

| part | how the value is set |
|---|---|
| role | the best `roles.preference` among the job's labels (ML 1.0, AI 0.95, DS 0.9, … DATA_ENGINEERING 0.5, ANALYTICS 0.35). +0.1 when the title is ≥90% similar to a preferred title |
| experience | required and within your limit: 1.0 (0 years or range ≤ limit), else `max(0.5, 1 − 0.1·years)`. "Prefers" above the limit: 0.55; "prefers" within it: 0.85. By level: entry 1.0, intern 0.9, mid 0.45, senior 0.25, not stated 0.5 |
| skills | `coverage = min(1, Σ your weight × w / Σ w × 1.15)`, where a nice-to-have skill has `w = 1/3` and a required one `w = 1`. Shrunk toward 0.5 when fewer than 5 skills are named. None named: 0.5 |
| location | preferred city 1.0 · remote and open to you 0.9 · remote, region unknown or closed 0.6 · a country on your list 0.75 · location unclear 0.5 · elsewhere 0.3 |
| visa | not needed there 1.0 · sponsorship stated 1.0 · **licensed sponsor on a register 0.8** · your company note 0.75 · unclear 0.45 · refused 0.2 |
| career | none of your `career_interests`: 0.25, else `min(1, 0.25 + 0.18·hits + 0.15 if one is in the title)` |
| company | target priority A 1.0 · B 0.75 · C 0.5 · not a target 0.3 |
| fit | the percentile of the TF-IDF cosine between the posting and your CV text, among all open jobs. No CV text: 0.5 |

---

## 4. How the weights were tuned (the method)

**Judgements used**
1. **Simon's ratings.** 👍/👎 on the cards (`job_review.feedback`, plus `feedback_reason` and
   `feedback_note` since schema v9), applications, and dismissals with a fit reason. As of 28 Sep:
   38 👍, 10 👎, and 1 application.
2. **Claude's ratings.** Simon asked Claude to rate jobs for him. Claude read 120 passing jobs in
   full and judged each against the CV, `config/my_profile.md`, the profile and Simon's 48 ratings.
   - **Sampling:** 24 jobs per score band (80+, 70–80, 60–70, 50–60, <50), at most 2 per company
     and 7 per source family per band, random seed 7.
   - **Result:** 28 good, 73 bad, 19 unsure. Unsure ones are never used.
   - **File:** `data/ratings/claude-2026-09-28.csv` (cluster_id, label, reason, note, title,
     company, score_when_rated). It is gitignored.
   - **Rubric, good:** ML, DS, research engineering, RL, simulation, forecasting, risk or quant
     work at intern-to-junior level, with more tolerance for seniority at AI labs, self-driving and
     motorsport companies.
   - **Rubric, bad:** specialist computer vision, 3D or graphics work; customer-facing roles
     (value engineer, forward-deployed, architect, pre-sales); pure software or systems work that
     wants publications; a PhD requirement; a language he doesn't speak; clearly 2+ years in
     industry; citizenship or clearance requirements; gig and task platforms.

**Algorithm** (`src/pitwall/jobsearch/calibrate.py`)
- **Examples:** only jobs that pass the hard filters. Features are the 8 part values stored in
  `job_matches.components`.
- **Per-part separation:** the AUC (Mann-Whitney), i.e. the chance that a good job's value beats a
  bad job's. 0.5 is a coin flip; 1.0 is perfect.
- **Fitting:** non-negative logistic regression by projected gradient descent (3,000 steps, rate
  0.5, L2 0.01), rescaled to the current total of 105. This gives the **fitted** weights.
- **Blending:** **blended** = ½ current + ½ fitted, because a few dozen examples shouldn't move a
  weight all the way.
- **With `--labels`:** the weights are fitted on Claude's labels and **tested on Simon's own
  ratings, which the fit never sees.** If both rated a job, Simon's rating wins and the job
  counts once.
- **Comparing weightings:** the non-weight adjustments are kept, so
  `new score = weighted average under new weights + (stored score − weighted average under current)`.
- **Decision rule, set before seeing any result:** apply only if the blended weights rank Simon's
  own ratings at least as well as the current ones. `job-calibrate --apply` refuses otherwise.

---

## 5. Results: every calibration run

AUC: 0.5 = coin flip, 1.0 = a perfect ranking.

**Run 1 (28 Sep): Simon's ratings only, before any fixes.**
39 good (38 👍 + 1 application), 10 bad. There were too few 👎 to fit weights (at least 15 needed).

| skills | location | visa | company | career | fit | role | experience | whole score |
|---|---|---|---|---|---|---|---|---|
| 0.78 | 0.59 | 0.53 | 0.49 | 0.48 | 0.41 | 0.38 | 0.36 | 0.67 (0.65 after the first fixes) |

**Run 2 (28 Sep): fitted on Claude's labels, tested on Simon's.**
This ran after the citizenship, clearance, PhD, fluency and optimisation-role fixes. Claude's
labels contributed 27 good and 65 bad that pass the filters; Simon's contributed 37 good and 6 bad.

| part | AUC (all labels) | now | fitted | blended |
|---|---|---|---|---|
| skills | 0.80 | 20 | 13.4 | 16.7 |
| career | 0.78 | 10 | 6.7 | 8.3 |
| fit | 0.74 | 5 | 9.5 | 7.2 |
| visa | 0.71 | 10 | 11.1 | 10.6 |
| location | 0.71 | 10 | 8.4 | 9.2 |
| company | 0.69 | 5 | 7.6 | 6.3 |
| experience | 0.67 | 20 | 29.5 | 24.8 |
| role | 0.61 | 25 | 18.7 | 21.9 |

- On Simon's ratings: now **0.59** · fitted 0.50 · blended 0.52, so **rejected**.
- On Claude's labels: now 0.89 · blended 0.89.

**Runs 3 to 5 (28 Sep): after each group of rule fixes.**

| after | Simon good/bad | Simon: now · fitted · blended | Claude: now · blended | verdict |
|---|---|---|---|---|
| customer-facing → OTHER, avoid topics, task-marketplace agencies | 37 / 5 | 0.65 · 0.61 · 0.63 | 0.90 · 0.89 | rejected |
| Singapore agency licences and names | 37 / 5 | 0.65 · 0.61 · 0.63 | 0.90 · 0.90 | rejected |
| degree fixes (normalizer `js9`) | 37 / 7 | **0.74** · 0.71 · 0.72 | 0.90 · 0.89 | rejected |

**Run 6 (29 Sep, today): after RocketApply's first day and runs #49–51.**

| part | AUC (all labels) | now | fitted | blended |
|---|---|---|---|---|
| career | 0.82 | 10 | 5.2 | 7.6 |
| fit | 0.79 | 5 | 12 | 8.5 |
| skills | 0.78 | 20 | 8.9 | 14.4 |
| visa | 0.74 | 10 | 11.5 | 10.8 |
| company | 0.72 | 5 | 6.8 | 5.9 |
| location | 0.71 | 10 | 8.9 | 9.4 |
| experience | 0.69 | 20 | 31.7 | 25.9 |
| role | 0.63 | 25 | 20 | 22.5 |

- **The tool reports:** Simon's ratings 51 good, 4 bad; now 0.97 · fitted 0.89 · blended 0.96.
  **This is inflated (§7.1).**
- **On Simon's explicit 👍/👎 only (36 good, 7 bad):** now **0.734** · fitted 0.623 ·
  blended 0.679, so **rejected**.
- **On Claude's labels (24 good, 60 bad):** now 0.92 · blended 0.91.

**How clean the top of the list is (share of Claude's decided labels that are good)**

| score band | when rated (28 Sep) | now (29 Sep) |
|---|---|---|
| 80+ | 64% (14 good / 8 bad) | **70%** (14 / 6) |
| 70–80 | 57% (12 / 9) | **67%** (12 / 6) |
| 60–70 | 11% (2 / 16) | 12% (2 / 15) |
| under 60 | 0% (0 / 40) | 0% (0 / 34) |

11 jobs labelled bad are now filtered out, and 0 labelled good.

**Caveat:** the rules in §6 were partly derived from Simon's ratings, so the rise on those same
ratings (0.59 → 0.73) is partly in-sample. The real test is whether new 👎 get rarer at the top.

---

## 6. What was applied instead

### 6.1 Changes that move a job's score (no weight changes)

| change | effect | where |
|---|---|---|
| New part `fit` ("CV match") | TF-IDF similarity between the posting and the CV text (`config/my_profile.md` plus profile experience, projects, evidence, education and summaries; never `personal.yaml`), as a percentile; weight 5 | `jobsearch/similarity.py`, `run.JobSearch.attach_cv_similarity` |
| Nice-to-have skills | a skill named only as "a plus / nice to have / bonus / differentiate…" counts 1/3 | `jobsearch/sections.py`, `scoring.skills_component`, column `jobs.skills_nice` |
| Sponsor registers | the visa part becomes 0.8 "licensed sponsor" when the UK (Skilled Worker route), NL (IND) or US (H-1B approvals) register names the employer for the job's country. Never for agency postings; the posting's own words always win | `jobsearch/sponsors.py`, data in `data/sponsors/` |
| Language levels | −8 × share (§3), capped at two languages' worth | `scoring.LEVEL_SHARE`, profile `languages_spoken` |
| Avoid topics | −10 when the title names one; never filters | `scoring.AVOID_PENALTY`, profile `avoid_topics` |
| Agency | −3. Now also covers task marketplaces (micro1, Mercor, Toptal, "per accepted task") and Singapore agencies (EA licence and registration numbers, "… Resources Pte", "Human Resource", "HR Advisory") | `normalize.is_agency` |
| Remote regions | a bare "Remote" means region unknown (location 0.6, "?"), not open everywhere; a list of countries is kept as a list; EU and NA count as regions, not countries | `locations.py`, `filters.remote_open_to_you` |
| US level suffixes | "Engineer I" = entry, "II" = left to the body, "III" = mid, "IV" and "5"+ = senior | `experience.title_level` |

### 6.2 Fixes that change which jobs pass the filters

| fix | real example it came from | where |
|---|---|---|
| Citizenship and clearance requirements become a refusal scoped to one country | "Singaporean ONLY", "TS/SCI", "must be a U.S. citizen", UK "SC Cleared / NPPV3" | `visa._CITIZENSHIP` |
| PhD in the title counts as required unless it says "MS/PhD" or "Master's or PhD" | Helsing "AI Research Intern (PhD)" | `normalize._PHD_TITLE` |
| A requirement line that opens with a degree counts as required, except under nice-to-have or "differentiate" headings; the lowest degree named decides | NTU "• PhD in Computer Science…" (required); a robotics posting with a Master's plus a PhD that "differentiates" (Master's) | `normalize.education`, `_EDU_LEAD` |
| "Read and write French", "Fluency in Dutch", within one clause only | Meetdavis, a Lisbon marketing role | `normalize.languages_required` |
| Customer-facing titles become role OTHER | Celonis "Value Engineer", Mistral "Forward Deployed", OpenAI "AI Architect" | `roles._CUSTOMER_FACING` |
| Marketing / resource "optimisation specialist" becomes role OTHER | "Dutch Marketing Optimisation Specialist", "Resource Optimization Specialist" | `roles._OPT_BUSINESS` |
| "(Based in Hong Kong)" in the title overrides the listed address; HK and CN added | Bank of China at a London career fair | `normalize._BASED_IN` |
| Role nouns: "Ai Engineer", co-op, placement, French "Stage" | Jooble and Adzuna titles | `roles.py` |
| Posting-language detection covers pt, es, it, nl, sv, da, fi | Swedish ads on JobTech | `normalize._STOPWORDS` |

### 6.3 Profile changes (`config/job_search.yaml`)

| field | before (28 Sep) | now |
|---|---|---|
| `work_authorization.authorized_countries` | [UA] | [PT, UA] |
| `roles.accepted` | 7 labels | + DATA_ENGINEERING, ANALYTICS (their preferences, 0.5 and 0.35, rank them lower) |
| `avoid_topics` | — | computer vision, 3d reconstruction, slam, computer graphics, cad |
| `languages_spoken` | [en] | en:C1, pt:B1, it:A2, ru:native, uk:native (Claude wrote en:C2; it has been C1 since the 29 Sep save) |
| `ai_fit_top_n` | 0 | 30 (the AI second opinion is shown beside the score, never inside it) |
| `weights` | 7 parts | the same values; `fit: 5.0` is now written out (§1) |
| `sources.*` | Arbeitnow, Adzuna | 13 sources plus discovered company boards (they affect which jobs exist, not the score) |

---

## 7. Known issues, for whoever works on RocketApply

**7.1 Calibration counts applications as "good", even over an explicit 👎.**
- **The logic:** `calibrate.examples()` treats any application row as good
  (`if feedback == "good_match" or applied`), whatever its status.
- **Today's data:** 49 rows have status CV_READY, all created at 13:18:31 on 29 Sep. Three of them
  are jobs Simon rated 👎: the two Waymo "Software Engineer" internships and Forto.
- **The effect:** RocketApply's "Measure the score against my ratings" shows 51 good, 4 bad and
  AUC 0.97. The honest figure from explicit ratings is 36 good, 7 bad and 0.73.
- **Suggested fix:**
  - explicit feedback always wins;
  - count as good only `good_match`, or a status meaning the application was sent (APPLIED,
    ONLINE_ASSESSMENT, INTERVIEW, FINAL_INTERVIEW, OFFER, and arguably REJECTED and WITHDRAWN,
    since applying was already decided);
  - SHORTLISTED, CV_READY and IN_PROCESS are intentions, not judgements, so don't count them.

**7.2 Already fixed in `227ae96`.** In Claude's language-level code, the language loop reused the
`missing` variable that held the missing skills. Scores were correct, but 2,401 `missing_skills`
lists held languages. Languages now go to `Match.missing_languages`.

**7.3 Old servers.** Two PitWall servers Claude had started on 28 Sep (on the old `app/Home.py`)
were still holding :8501 and `127.0.0.1:8502`. They were stopped on 29 Sep, so RocketApply alone
answers on :8502. Start PitWall with `make pitwall`.

---

## 8. Check it yourself

```bash
# 1. The weights in force, and their fingerprint
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from pitwall.jobsearch.settings import load_config as l; c=l(); print(c.weights); print(c.fingerprint())"
```

```bash
# 2. Were all open jobs scored with them? Expect one row whose hash equals that fingerprint
sqlite3 data/pitwall.db "SELECT m.config_hash, COUNT(*) FROM job_matches m JOIN jobs j ON j.id = m.job_id WHERE j.status = 'open' GROUP BY 1"
```

```bash
# 3. The value and weight of every part behind one stored score
sqlite3 data/pitwall.db "SELECT score, components FROM job_matches WHERE job_id = 28019"
```

```bash
# 4. Calibration, your ratings only, then fitted on Claude's labels and tested on yours
make job-calibrate
```

```bash
.venv/bin/python scripts/run_pipeline.py job-calibrate --labels data/ratings/claude-2026-09-28.csv
```

On 29 Sep, check 1 printed the weights of §1 and `f3b6227f4461`. Check 2 printed one row,
`f3b6227f4461 | 38435` (scored at 15:16–15:17 in run #51); older hashes exist only on closed jobs.

**Worked examples** (config_hash `f3b6227f4461`; value × weight per part, total weight 105):

| job | role | exp | skills | loc | visa | career | company | fit | base | adjustment | stored |
|---|---|---|---|---|---|---|---|---|---|---|---|
| #28019 GRiT, Data Scientist – Risk & Pricing Models | 1.000×25 | 0.500×20 | 1.000×20 | 1.000×10 | 1.000×10 | 0.940×10 | 0.300×5 | 0.886×5 | 86.0 | 0 | **86.0** |
| #27984 Eurotux, Junior AI Engineer | 1.000×25 | 1.000×20 | 0.889×20 | 0.750×10 | 1.000×10 | 0.250×10 | 0.300×5 | 0.284×5 | 81.6 | −4.0 (Portuguese, B1) | **77.6** |
| #19661 Imnoo, ML / Computer Graphics Engineer | 1.000×25 | 1.000×20 | 0.683×20 | 0.900×10 | 1.000×10 | 0.610×10 | 0.300×5 | 0.963×5 | 85.8 | −10.0 (avoid: computer graphics) | **75.8** |
| #22719 JAC Recruitment, Applied AI Researcher | 1.000×25 | 0.800×20 | 0.920×20 | 0.750×10 | 0.450×10 | 0.790×10 | 0.300×5 | 0.999×5 | 81.7 | −3.0 (agency) | **78.7** |

The same inputs must give the same scores in any app that claims to use this logic.

**To apply weights yourself:**
1. Change them, by any one of these:
   - edit `weights:` in the YAML;
   - save from PitWall's Job Setup page;
   - run `job-calibrate --labels … --apply`, which writes the blended weights, but only if they
     pass the §4 rule.
2. Re-rank: `make job-rerank`, or RocketApply's re-rank, which calls `JobSearch().rerank()`.
3. Confirm with check 2: every open job should carry the new fingerprint.

---

## 9. Files and commits

- **Scoring and weights:** `src/pitwall/jobsearch/scoring.py`, `settings.py`, `calibrate.py`,
  `similarity.py`, `sponsors.py`, `sections.py`.
- **Extraction and filters:** `normalize.py` (normalizer version `js9`), `filters.py`,
  `locations.py`, `experience.py`, `roles.py`, `visa.py`.
- **Orchestration:** `run.py`, `market.py` (the source registry), `queries.py`.
- **Commits:** `16b36f4` "feat(js.9)…" (29 Sep, 09:32) holds all of the above;
  `227ae96` holds the scorer fix in §7.2. Both are on `job-search` and `split`.
- **Ratings:** Simon's are in `job_review` (in `data/pitwall.db`); Claude's are in
  `data/ratings/claude-2026-09-28.csv`.
