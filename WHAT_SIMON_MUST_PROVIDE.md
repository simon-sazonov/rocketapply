# What You Need to Provide — Profile Data Specification

**Audience:** you (Simon), before the developer can build anything useful.
**Companion document:** `CV_COVER_LETTER_ENGINEERING_SPEC.md` (the technical build spec).

---

## 0. Read this part first

The system cannot invent anything about you. That is the whole design — it is what stops it lying on your behalf. The direct consequence:

> **The quality of every CV and cover letter it produces is capped by the quality of what you write in `profile/`.**

If you write eight vague bullets, you will get vague applications forever, and no model upgrade will fix it. If you write forty precise, tagged, evidence-backed bullets, a small local model will produce applications that beat what most people write by hand.

**This is the highest-leverage work in the entire project.** Budget a real day for it — roughly 6–8 focused hours. You do it once, then top it up every few months.

A rough split of where that time goes:

| Task | Time |
|------|------|
| Experience bullets (the hard part) | 2 h |
| Projects | 2 h |
| Skills + honest confidence levels | 1 h |
| Evidence blocks for cover letters | 1.5 h |
| Summary variants, style samples, forbidden list | 1 h |

---

## 1A. Your CV layout is FROZEN — read this before writing anything

Your existing CV — the Canva design you shared — is now the **fixed frame**. Every application uses that exact layout: same sections, same order (`CONTACT → SKILLS → EXPERIENCE → PROJECTS → EDUCATION`), same experience block, same education block, one page. Section labels sit in the narrow left gutter; all content sits in the right column. Nothing moves.

Only **three things** change from job to job:

```text
┌──────────────┬───────────────────────────────────────────────┐
│              │ Simon Sazonov                  Data Scientist │  ← never changes
│  CONTACT     │ Email / GitHub / LinkedIn                     │  ← never changes
│  SKILLS      │ • programming languages        1 line         │  ◄── ② WHICH ITEMS
│              │ • methods & techniques         2 lines        │       fill each row
│              │ • tools & libraries            2 lines        │
│              │ • spoken languages             1 line         │  ← never changes
│  EXPERIENCE  │ Ophis Research                 3 bullets      │  ← always all three,
│              │ FreedomCode                    3 bullets      │     same counts;
│              │ BeeSurfing                     1 bullet       │     ③ wording may swap
│  PROJECTS    │ project 1                      2 bullets      │  ◄── ① WHICH 3, from
│              │ project 2                      2 bullets      │       your library
│              │ project 3                      2 bullets      │
│  EDUCATION   │ Nova (MSc + BSc), Milano, Lyceum              │  ← never changes
└──────────────┴───────────────────────────────────────────────┘
                                                                  ◄── ③ experience bullet
                                                                       wording (rare)
```

| What | Changes per job? | Your job when writing the profile |
|------|------------------|-----------------------------------|
| **Projects** — which 3 appear | **Yes** | Write **8–12 projects** so there is a real choice. This is the biggest change from what you have now. |
| **Skills** — what fills each row | **Yes** | Write the **full master list** per row, far more than fits on one line. |
| **Experience** — wording | Rarely | Write **line-neutral variants** for the 3–4 bullets whose framing genuinely shifts by role. |
| Experience — which jobs, how many bullets | **No** | Lock Ophis = 3 bullets, FreedomCode = 3, BeeSurfing = 1. Write exactly those. |
| Education | **No** | Transcribe once, never touch again. |
| Section order, header, contact | **No** | Transcribe once. |

> **One job that is yours, not the developer's:** the Canva design cannot be filled by code — Canva's template API needs an Enterprise/Teams plan. The developer will rebuild your layout as an HTML/CSS template that renders to PDF. Give them the Canva link (done), plus **File → Download → PDF Print** and a full-zoom screenshot so they can match spacing exactly. Also tell them whether you hold a licence for **Machina**, the commercial font in your name and title — if not, they substitute once at the template level.

### The two hard rules this creates

**Rule 1 — character limits are real, not advice.**

The frame has no slack. Text that runs one character too long wraps to a new line and pushes the page to two. So:

| Text | Hard limit | Why |
|------|-----------|-----|
| Project bullet | **≤ 95 characters** | keeps it on one line |
| Experience bullet | must match your current CV's line count exactly | the frame is calibrated to it |
| Skills row 1 (Programming Languages) | ≤ 95 characters total | 1 line |
| Skills rows 2 and 3 | ≤ 195 characters total | 2 lines |
| Project title line | ≤ 90 characters including the GitHub label | 1 line |

Count them. `len(text)` in Python, or any character counter. A 97-character bullet is a bug.

**Rule 2 — a rephrasing must occupy the same number of lines as the original.**

Your Ophis bullet 1 is currently 165 characters, which renders as 2 lines. Any variant you write for it must also land between 98 and 194 characters. A 90-character variant would shrink the block and leave a gap; a 200-character one would overflow the page.

The validator enforces this. A variant with the wrong line count is rejected before it can ever be used.

### Your project slot has a budget of 11 lines

Three projects share 11 lines. A project costs `1 (title) + lines(bullet 1) + lines(bullet 2)`.

- Both bullets ≤ 95 chars → **3 lines**. Safe, always fits.
- One bullet 96–192 chars → **4 lines**.
- Both bullets long → **5 lines**. Two of these and nothing else fits.

**Write every project with two ≤95-character bullets.** Three projects × 3 lines = 9, comfortably inside 11. Do that and the selector can pick any combination it likes, which is exactly what you want. Every long bullet you write removes combinations from the pool.

---

## 1. Format rules

- **Everything in YAML**, UTF-8, one file per section, in a folder called `profile/`.
- YAML because you can read and edit it without tools, and the program can validate it. Not Word, not PDF, not a Notion page.
- **Every record needs a stable `id`.** Lowercase, dots and underscores only: `ophis.b1`, `proj.wikiart.b3`. Once an ID exists, never rename it — old applications reference it.
- **Write in the first person past tense, without "I".** `"Developed a Monte Carlo risk model..."` — this is how it will appear on the CV.
- **Do not write marketing.** Write what you actually did, with enough specificity that you could answer a follow-up question about it in an interview. If you cannot defend a sentence for two minutes under questioning, delete it.
- If you are unsure about a fact, mark it. There is a `confidence` field for exactly this. Guessing high is the one mistake that damages you.

> **The interview test:** every single line in your profile will be read back to you by someone who wants to probe it. Write only lines you want that conversation about.

---

## 2. The files you must produce

```text
profile/
├── personal.yaml      # contact details
├── education.yaml     # degrees, thesis, relevant coursework
├── experience.yaml    # jobs, internships — with tagged bullets
├── projects.yaml      # personal/academic projects — with tagged bullets
├── skills.yaml        # skills WITH honest confidence + evidence links
├── evidence.yaml      # narrative blocks for cover letters
├── layout.yaml        # the frozen CV frame (see §1A) — written once, from your PDF
├── summaries.yaml     # optional: NOT used by the CV (it has no summary block)
├── style.yaml         # your writing voice + phrases you hate
├── forbidden.yaml     # things that must never appear
└── companies.yaml     # (optional, grows over time) notes on target employers
```

---

## 3. `personal.yaml`

```yaml
name: Simon Sazonov
email: sazonov.simon@gmail.com
phone: "+351 ..."                 # quoted, so YAML keeps the +
location: Lisbon, Portugal
willing_to_relocate: true
relocation_targets: [Germany, Netherlands, Portugal, remote-EU]
linkedin: https://linkedin.com/in/...
github: https://github.com/...
portfolio: ""                      # leave empty rather than deleting the key
work_authorization: "EU citizen / Portuguese residence permit — no sponsorship required"
languages:
  - {name: English, level: C2}
  - {name: Portuguese, level: B1}
  - {name: Russian, level: native}
notice_period: "immediately available"
```

**Why `work_authorization` matters:** many EU postings gate on it. Having it as a field lets the letter mention it in one clean sentence when the posting raises it, and stay silent when it does not.

---

## 4. `education.yaml`

```yaml
- id: edu.msc
  degree: MSc in Data Science and Advanced Analytics
  institution: NOVA Information Management School
  location: Lisbon, Portugal
  start: 2024-09
  end: 2026-07                     # or "present"
  grade: ""                        # only fill if it helps you — omit if not
  thesis:
    title: "Risk-aware race strategy optimisation under disruption"
    supervisor: ""
    summary: >
      Probabilistic modelling of sequential pit-stop decisions under safety-car
      and weather disruptions, combining simulation with machine learning.
    tags: [probability, optimization, simulation, machine_learning,
           reinforcement_learning, motorsport, research]
  relevant_courses:
    - {name: Machine Learning, tags: [machine_learning, python]}
    - {name: Statistical Modelling, tags: [statistics, r]}
    - {name: Deep Learning, tags: [deep_learning, pytorch]}
    - {name: Database Systems, tags: [sql, databases]}
    - {name: Optimization, tags: [optimization]}
```

**Do list relevant coursework.** For a graduate role it is legitimate evidence, and it lets the matcher find real support for a requirement instead of returning "missing". Tag each course, because that is how the matcher sees it.

**Do not** list every course you took. Six to eight that map to the roles you want.

---

## 5. `experience.yaml` — the most important file

### 5.1 Structure

```yaml
- id: ophis
  type: experience
  org: Ophis Research
  role: Data Science Intern              # EXACT title that goes on the CV
  location: Remote
  start: 2025-02
  end: 2025-08
  context: >
    Quantitative research team building ML-driven tooling for trading strategy
    evaluation. Worked with two researchers; my scope was modelling, not deployment.
  bullets:
    - id: ophis.b1
      text: >
        Developed a Monte Carlo risk model estimating probabilities of stop-loss
        and take-profit outcomes across simulated price paths.
      tags: [python, monte_carlo, probability, risk_modeling, simulation,
             quantitative, machine_learning, finance]
      metrics: []
      strength: strong
      use_for: [data_science, quantitative, machine_learning, research]
      never_use_for: []
      variants:
        - id: ophis.b1.v2
          text: >
            Built a Monte Carlo simulation quantifying stop-loss / take-profit
            outcome probabilities under stochastic price dynamics.
```

### 5.2 How to write a good bullet

The shape that works:

> **[Action verb] [what you built] [how / with what] [why it mattered or what it produced]**

Compare:

| Weak | Strong |
|------|--------|
| "Worked with machine learning models" | "Built and evaluated gradient-boosted models to predict X, benchmarked against a logistic baseline" |
| "Used SQL" | "Rewrote reporting queries with composite indexes, cutting execution from 15,832 ms to 63 ms" |
| "Helped the team with data" | "Built the ingestion pipeline that reduced weekly manual data prep from 4 hours to 20 minutes" |
| "Responsible for analysis" | "Analysed 1.2M trade records to quantify slippage by order size, informing execution thresholds" |

Each bullet should be **one sentence, 90–160 characters**. Longer and it wraps to three lines and destroys the page budget. Shorter and it says nothing.

### 5.3 How many bullets — FIXED, because the frame is fixed

This is the part that differs from ordinary CV advice. Your experience block never changes shape:

| Entry | Bullets | Always shown? |
|-------|---------|---------------|
| Ophis Research | exactly **3** | yes |
| FreedomCode | exactly **3** | yes |
| BeeSurfing | exactly **1** | yes |

Write exactly those seven bullets. There is no selection here — all seven print on every application. So make each one carry its weight, because none of them can be swapped out for a better-matching one.

**Where tailoring happens instead is `variants` (§5.6).** If your Ophis work reads differently to a quant fund than to an automotive ML team, that difference lives in two pre-approved phrasings of the same bullet, not in two different bullets.

> **Contrast with projects:** projects *are* selected (3 of 8–12), so write far more projects than fit. Experience is fixed; projects are the variable. Put your writing effort where the selection happens.

### 5.4 The `strength` field

- `strong` — headline material, you would lead with it
- `medium` — good supporting evidence
- `supporting` — include only when it directly matches a requirement

### 5.5 `use_for` / `never_use_for`

Coarse gating by role family. Use these values: `data_science`, `machine_learning`, `data_engineering`, `analytics`, `research`, `quantitative`, `software`.

`never_use_for` is for genuine mismatches — e.g. a heavily finance-flavoured bullet you do not want leading an automotive application. Use it sparingly; over-gating starves the selector.

### 5.6 `variants` — your secret weapon

A variant is **a rephrasing you have pre-approved**. The program can swap in a variant to match a job's vocabulary without any model inventing anything.

Write a variant when a bullet has a natural alternative framing:

```yaml
- id: proj.wikiart.b1
  text: "Built and evaluated CNN models for artwork style classification on 80k images."
  variants:
    - id: proj.wikiart.b1.v2     # for computer-vision roles
      text: "Trained convolutional neural networks for image classification across 80k artworks, comparing architectures on held-out accuracy."
    - id: proj.wikiart.b1.v3     # for generalist data-science roles
      text: "Applied deep learning to a large-scale image classification problem, with systematic model evaluation."
```

**The line-neutrality rule:** a variant must render to the same number of lines as the original, or it breaks the frame. In practice:

| Original length | Variant must be |
|-----------------|-----------------|
| ≤ 97 chars (1 line) | ≤ 97 chars |
| 98–194 chars (2 lines) | 98–194 chars |
| 195–291 chars (3 lines) | 195–291 chars |

The validator rejects variants that fail this, so you will find out immediately rather than discovering a two-page CV after you have sent it.

Two variants on your three or four strongest bullets is plenty. Do not do this for every bullet.

### 5.7 Include everything, even the unglamorous

Teaching assistant work, freelance, volunteering, a hackathon, a society role. Tag it honestly. It may never be printed — but when a posting asks for "stakeholder communication" or "mentoring", having a real record beats having a gap.

---

## 6. `projects.yaml` — where most of your effort should go

**This is the variable slot. Write 8–12 projects.** Exactly 3 print on any given CV, chosen by relevance to the posting. You currently have 3, which means every CV prints the same three — no tailoring at all.

Hard shape requirements, non-negotiable because of the frame:

- **Exactly 2 bullets per project.** Not one, not three.
- **Each bullet ≤ 95 characters.** This keeps the project at 3 rendered lines.
- **Title line ≤ 90 characters** including any `GitHub` / `(walkthrough on request)` label.
- The three chosen projects share an **11-line budget**. Two ≤95-char bullets per project = 3 lines each = 9 total, which always fits. Exceed 95 characters and you start removing combinations the selector can choose from.

Same structure as experience, plus a few extra fields:

```yaml
- id: proj.airbnb
  type: project
  name: Airbnb Lisbon Database & Query Optimisation
  context_type: academic              # academic | personal | hackathon | freelance
  period: 2025-03
  url: https://github.com/...         # empty string if none — be honest
  stack: [PostgreSQL, Python, Pandas]
  summary: >
    Relational database design and query optimisation over Lisbon Airbnb listings.
  bullets:
    - id: proj.airbnb.b2
      text: >
        Reduced query execution time from 15,832 ms to 63 ms through composite
        indexing and query restructuring.
      tags: [sql, databases, optimization, performance, data_engineering]
      metrics: [m.airbnb.query_time]
      strength: strong
      use_for: [data_engineering, data_science, analytics]
```

**`context_type` matters.** It prevents a letter implying an academic project was professional work. That specific overstatement is both easy to make and very damaging in an interview.

**On GitHub links:** if a repo is linked on your CV, it will be opened. Make sure it has a README that explains the problem and the result. An empty repo is worse than no link.

---

## 7. Metrics — write these very carefully

Any number that will ever appear in a document needs a record:

```yaml
metrics:
  - id: m.airbnb.query_time
    claim: "reduced query execution time from 15,832 ms to 63 ms"
    raw_before: 15832
    raw_after: 63
    unit: ms
    method: "composite B-tree index on (neighbourhood, price); measured via EXPLAIN ANALYZE, mean of 20 runs"
    verifiable: true

  - id: m.wikiart.accuracy
    claim: "achieved 78% top-1 accuracy across 12 style classes"
    raw_value: 0.78
    unit: accuracy
    method: "ResNet-50 fine-tuned, held-out test set of 8,000 images"
    verifiable: true
```

Three rules:

1. **`claim` is copied word-for-word into documents.** Write it exactly as you want it printed.
2. **`method` is for you, not the CV.** It is the answer you give when an interviewer asks "how did you measure that?" Writing it now forces you to check the number is real.
3. **If you want a derived form ("251× faster", "over 99% reduction"), add it as its own metric record.** The program will never calculate one, by design — derived numbers are where exaggeration creeps in.

**If you cannot substantiate a number, do not write it.** A bullet with no number and a true story beats a bullet with an impressive number you cannot defend.

Aim for **3–6 real metrics total**. Most people have fewer than they think. That is fine.

---

## 8. `skills.yaml` — four fixed rows, variable contents

Your CV's skills block has **four rows with fixed line counts**. The program packs each row to a character budget, choosing the most job-relevant items first:

| Row | Lines | Budget | Varies? |
|-----|-------|--------|---------|
| Programming Languages | 1 | ≤ 95 chars | yes |
| Methods & Techniques | 2 | ≤ 195 chars | yes |
| Tools & Libraries | 2 | ≤ 195 chars | yes |
| Spoken Languages | 1 | ≤ 105 chars | **no — fixed text** |

**The master list per row must be far longer than what fits.** Row 3 currently holds 19 tools in ~185 of 195 available characters — it is effectively full, so nothing can be swapped in and that row will look identical on every application. Grow it to 30+ entries and it genuinely tailors.

Give every skill a `row`, a `pin` flag (always include, e.g. Python and SQL), and the confidence metadata below.

### Being brutally honest about confidence

```yaml
- id: skill.python
  name: Python
  category: programming
  confidence: high
  evidence: [ophis.b1, ophis.b3, proj.wikiart.b1, proj.airbnb.b1, edu.msc]
  years: 4
  aliases: [python3]
  printable: true

- id: skill.pytorch
  name: PyTorch
  category: ml
  confidence: medium
  evidence: [proj.wikiart.b1]
  years: 1
  printable: true

- id: skill.docker
  name: Docker
  category: tools
  confidence: low
  evidence: [edu.msc]
  low_phrasing: "familiar with containerised workflows from coursework"
  printable: false

- id: skill.aws
  name: AWS
  category: tools
  confidence: none
  evidence: []
  status: NOT_SUPPORTED
  printable: false
```

### The confidence ladder — and what each level actually means

| Level | The honest test | How it gets used |
|-------|----------------|------------------|
| `high` | You have used it substantively in **2+ pieces of work** and could be interviewed on it for 30 minutes | Appears in the skills block; can headline a cover letter |
| `medium` | **One** real piece of work, or limited scope | Appears in the skills block; in a letter, only ever named alongside that specific project |
| `low` | Coursework or brief exposure only | Only appears when the job explicitly asks, and only using your exact `low_phrasing` |
| `none` | No evidence at all | **Never appears.** Also actively blocked — see below |

### Why listing `none` skills is worth your time

This feels counter-intuitive: why list AWS if you do not have it?

Because it creates an **active block**. When a posting asks for AWS, the system adds it to a forbidden list for that run and the fact checker rejects any letter mentioning it. Without the record, the system only knows "no match found" — a softer signal.

List the 10–15 skills you keep seeing in postings but genuinely do not have. Docker, Kubernetes, AWS, Azure, Spark, Airflow, Tableau, Power BI, Snowflake, dbt — whichever apply. This is a **gap map**, and it doubles as your learning roadmap.

### Every skill needs `evidence`

If you cannot point to a bullet ID for a skill, its confidence is not `high`. This rule alone eliminates the most common CV lie — the skills list nothing in the body supports.

### Use the canonical tag vocabulary

Ask the developer for `taxonomy.py`'s canonical list and use exactly those strings in `tags:`. Not `ML` in one place and `machine learning` in another — matching works on exact canonical terms. If a tag you need is missing from the list, ask for it to be added rather than inventing a variant.

---

## 9. `evidence.yaml` — narrative blocks for cover letters

This is different from CV bullets. CV bullets are one-liners; evidence blocks are **stories with facts attached**, and the model turns them into prose.

Write **5–8 blocks**, one per significant piece of work.

```yaml
- id: ev.race_strategy
  title: Risk-aware race strategy under disruption
  source_refs: [edu.msc]
  facts:
    - researching sequential pit-stop decisions under uncertainty
    - combining probabilistic modelling with machine learning
    - modelling disruptions such as safety cars and weather changes
    - working with historical race telemetry and timing data
  skills: [probability, optimization, simulation, machine_learning, time_series]
  themes: [decision_under_uncertainty, applied_research, real_time_decisions]
  use_for: [data_science, machine_learning, research, quantitative]
  strength: strong
  one_line_hook: >
    Most models optimise for the expected case; races are won and lost on what
    happens when the expected case breaks.
  story: >
    The interesting part is that the optimal strategy changes completely the
    moment a safety car appears, and you have seconds to decide. That forced me
    to think about decisions as sequential and risk-weighted rather than as a
    single prediction.
```

### Field guide

| Field | What to write |
|-------|--------------|
| `facts` | **The only things the model may assert about this block.** 3–6 short, plain, verifiable statements. No adjectives. |
| `themes` | The abstract ideas this demonstrates. This is how the system connects your motorsport thesis to a predictive-maintenance job. |
| `one_line_hook` | A sentence that makes someone curious. Your voice, not a model's. |
| `story` | 2–4 sentences of genuine insight — what was hard, what surprised you, what you learned. |

### The `story` field is what stops letters sounding like AI

A model given only "built a Monte Carlo risk model" writes a generic paragraph. Given *"the hard part was that tail risk is where all the money is, and the tail is exactly where you have the least data"* — it has something real to work with.

**Write the `story` fields as if explaining to a smart friend over coffee.** Rough, honest, specific. Do not polish them. This is the single field that most improves output quality, and almost nobody writes it.

### Also write these two generic blocks

```yaml
- id: ev.why_me_graduate
  title: Where I am and what I am looking for
  facts:
    - finishing an MSc in Data Science and Advanced Analytics
    - looking for a first full-time role in applied data science
  themes: [motivation, trajectory]
  story: >
    I want to work somewhere the models actually get used for decisions, not
    somewhere they end in a slide deck. That is what pulled me toward
    simulation and risk work in the first place.

- id: ev.working_style
  title: How I work
  facts:
    - comfortable working independently on open-ended problems
    - used to translating a vague question into something measurable
  themes: [autonomy, problem_framing]
```

These fill the "why you" paragraph without the model inventing a personality for you.

---

## 10. `summaries.yaml` — pre-written CV summary variants

The 2–3 line block at the top of the CV. **You write these, not the model.** Write 4–6, one per role family you target.

```yaml
- id: sum.ds_applied
  for: [data_science, analytics]
  text: >
    MSc Data Science graduate with hands-on experience building predictive and
    probabilistic models in Python and SQL. Comfortable taking a vague question
    to a measurable answer, and honest about what the data does not support.

- id: sum.ml_engineering
  for: [machine_learning]
  text: >
    MSc Data Science graduate with project and internship experience building and
    evaluating machine learning models in Python and PyTorch, from computer vision
    to simulation-based risk modelling.

- id: sum.quantitative
  for: [quantitative, research]
  text: >
    MSc Data Science graduate focused on decision-making under uncertainty —
    Monte Carlo risk modelling, probabilistic simulation, and sequential
    optimisation applied to real operational problems.
```

Rules: 2–3 lines maximum, no "passionate", no "results-driven", no "dynamic professional". Say what you can do and what you are looking for.

---

## 11. `style.yaml` — your writing voice

This is how you stop every letter reading like every other AI letter.

```yaml
voice_notes:
  - direct, plain sentences; no corporate padding
  - specific over enthusiastic — name the problem, not the feeling
  - comfortable admitting limits; it reads as confidence
  - British spelling (optimise, modelling, analyse)
  - short paragraphs, no more than 4 sentences

banned_phrases:
  - "I am writing to apply"
  - "I was excited to see"
  - "passionate about"
  - "perfect fit"
  - "proven track record"
  - "leverage my skills"
  - "I believe my background makes me"
  - "dynamic environment"
  - "wealth of experience"
  - "as a highly motivated individual"
  - "delve"
  - "in today's fast-paced world"
  - "align with your needs"

preferred_openings:
  - name a specific thing about the company or the problem, then connect it to your work
  - open with the problem you find interesting, then say why this role touches it

writing_samples:
  - >
    Paste 2–4 paragraphs here that you actually wrote — from a cover letter you
    were happy with, a project README, a thesis abstract, a LinkedIn post. Real
    text, unedited. This is the single most useful thing in this file: the model
    copies rhythm and register from examples far better than from adjectives.

length:
  cover_letter_words: 280-330
  cover_letter_paragraphs: 4
  cv_pages: 1
```

**Do the `writing_samples` properly.** Instructions like "write naturally" do almost nothing. Two paragraphs of your real writing do a great deal.

---

## 12. `forbidden.yaml` — the do-not-claim list

```yaml
never_claim:
  - production ML deployment
  - MLOps ownership
  - team leadership
  - managing people
  - years of professional experience beyond internships
  - client-facing consulting
  - published research
  - AWS / Azure / GCP hands-on work
  - Kubernetes
  - Spark at scale

never_say:
  - anything implying the Ophis work was full-time permanent employment
  - anything implying academic projects were commercial work
  - anything implying I have shipped a model to production users

gated_verbs:
  - led
  - owned
  - managed
  - architected
  - spearheaded
  - directed
```

The `gated_verbs` list matters more than it looks. Small models reach for "led the development of" as filler. It is technically a lie for an intern, and interviewers notice.

---

## 13. `companies.yaml` — optional, grows as you go

The weakest paragraph in any generated letter is "why this company", because the model only has the job posting. Two or three sentences from you fixes it.

```yaml
- id: co.bmw
  name: BMW Group
  notes: >
    Their Munich data team works on predictive maintenance and production-line
    optimisation. The public work on battery-cell degradation modelling is close
    to my thesis: both are risk-weighted decisions on time-series data.
  angle: >
    Same class of problem as my thesis — predicting when something breaks and
    deciding what to do about it before it does.
  source_urls: [https://...]
```

Add an entry when you are genuinely interested in a company. Fifteen minutes of your reading produces a paragraph no model could have written.

---

## 14. The jobs CSV

Tell the developer your exact column names. The minimum the pipeline needs:

| Column | Required | Notes |
|--------|----------|-------|
| `title` | yes | job title |
| `company` | yes | |
| `location` | yes | drives relocation notes |
| `description` | **yes** | **full text, not a snippet** — this is the critical one |
| `url` | yes | for provenance |
| `posted_date` | no | for prioritisation |
| `source` | no | LinkedIn / Indeed / company site |
| `job_id` | no | generated if absent |

**If your CSV only stores a truncated description, fix that before anything else.** Every downstream stage reads the full posting. A 200-character snippet produces a 200-character-quality application.

---

## 15. Existing documents to hand over

Give the developer these as **raw material** for populating `profile/` — not as inputs the program reads at runtime:

1. Your current CV (DOCX preferred over PDF — the text extracts cleanly)
2. Two or three cover letters you have actually sent, ideally ones that got a response
3. Your LinkedIn export (Settings → Get a copy of your data)
4. Your thesis abstract / proposal
5. READMEs from your significant repos
6. Any performance review, internship feedback, or reference letter — these often contain phrasing about you that you would never write yourself, and it is *verified* phrasing

---

## 16. Quality checklist — go through this before handing over

Frame compliance (check these first — they are pass/fail):

- [ ] `experience.yaml` has exactly 3 entries with exactly 3 / 3 / 1 bullets
- [ ] Every experience `variant` has the same line count as its canonical bullet
- [ ] `projects.yaml` has **at least 8** projects
- [ ] Every project has **exactly 2** bullets
- [ ] Every project bullet is **≤ 95 characters** (count them)
- [ ] Every project title line is ≤ 90 characters including its link label
- [ ] Skills row 1 master list exists; rows 2 and 3 have **30+** candidates each
- [ ] Row 4 (spoken languages) is a single fixed string ≤ 105 characters
- [ ] No summary/profile section has been added

Data completeness:
- [ ] Every bullet has **at least 3** tags
- [ ] Every tag is from the canonical taxonomy
- [ ] Every `high`-confidence skill lists **2+** evidence IDs
- [ ] Every number in the profile has a `metrics` record with a `method`
- [ ] Every ID is unique and matches `^[a-z0-9_.]+$`
- [ ] Every `evidence:` reference points at a real bullet ID

Honesty:

- [ ] I could be interviewed for 30 minutes on every `high`-confidence skill
- [ ] Nothing implies an academic project was commercial work
- [ ] Nothing implies an internship was a permanent role
- [ ] Every metric is one I could reproduce or explain
- [ ] The `forbidden.yaml` list covers everything I keep seeing in postings and do not have

Quality:

- [ ] Project bullets are ≤ 95 characters; experience bullets match their current line counts
- [ ] Each bullet says what was done, how, and why it mattered
- [ ] The same work is framed from 2–3 different angles across different *projects* and *variants*
- [ ] Every evidence block has a real `story` field in my own words
- [ ] `style.yaml` contains genuine writing samples, not a description of my style
- [ ] Projects cover distinct themes, not three variations on "I built a pipeline"

---

## 17. The five mistakes that will waste your time

**1. Writing only the projects that are already on your CV.**
Three projects means every application prints the same three. The project library is the main variable in the whole system — it needs 8–12 entries before tailoring means anything. This is the single highest-value hour of the whole exercise.

**2. Inflating confidence levels.**
"I did a tutorial" is not `medium`. Mislabelled confidence is the one thing that lets a false claim reach a real employer, and the system trusts your labels absolutely.

**3. Vague tags.**
`[data, coding, analysis]` matches nothing useful. `[python, sql, time_series, optimization]` matches jobs. Tags are the matching mechanism, not decoration.

**4. Skipping the `story` and `writing_samples` fields.**
They feel optional and soft. They are the difference between a letter that reads like a person and a letter that reads like a language model. Take them seriously.

**5. Writing it once and never touching it again.**
Every project you finish, every tool you genuinely learn, is a new record. Fifteen minutes a month. A stale profile decays quietly — you will not notice until the output is generic.

---

## 18. Do it in this order

1. **`personal.yaml` + `education.yaml`** — 30 min, mechanical, gets you moving
2. **`experience.yaml`** — 2 h, the hard part, do it when fresh
3. **`projects.yaml`** — 2 h — **the important one.** Transcribe the 3 you have, then write 5–9 more. Count characters as you go.
4. **`skills.yaml`** — 1 h, including the honest `none` list
5. **Metrics pass** — 30 min, go back through and substantiate every number
6. **`evidence.yaml`** — 1.5 h, write the `story` fields properly
7. **`style.yaml` + `forbidden.yaml`** — 1 h (skip `summaries.yaml`; your CV has no summary block)
8. **Run `app validate-profile`** and fix everything it complains about
9. **Generate three applications for jobs you know well** and read them critically — where the output is thin, the profile is thin, and you will see exactly which record to improve

Step 9 is the real feedback loop. The first three generated applications will teach you more about what your profile is missing than any amount of planning.

---

## 19. The short version

> Write down everything true about your work, in small tagged pieces, with honest confidence levels and real stories behind them.
>
> The program will never add anything you did not write — so the only question that matters is whether what you wrote is complete, precise, and true.

---

## 20. Appendix — your current CV, already transcribed into the schema

This is your existing CV converted into the profile format. **It is a starting point, not finished work.** Copy it into `profile/`, then:

- ✅ **Verify every tag.** I inferred them from the text; you know what you actually did.
- ✅ **Add the `metrics` records** for every number (ROC-AUC 0.878, RMSE 73%, ~20%, 0.62→0.94, 0.75/0.82, 260 tests / 90% coverage, 74 years / 14 tables).
- ✅ **Add 5–9 more projects** so the top-3 selection has something to select from.
- ✅ **Expand the skills master list** well beyond what currently prints.
- ✅ **Write the `evidence.yaml` story fields** — none of that exists yet and it is what makes the cover letters sound like you.

`char` counts are given so you can see which bullets are at the line boundary.

### `layout.yaml` — measured from your PDF

```yaml
page: {size: A4, max_pages: 1, chars_per_line_bullet: 97, chars_per_line_fullwidth: 105}
sections:
  - {id: contact,    fixed: true}
  - {id: experience, fixed: true, line_budget: 17,
     entries: [{id: ophis, bullets: 3}, {id: freedomcode, bullets: 3}, {id: beesurfing, bullets: 1}]}
  - {id: education,  fixed: true, line_budget: 14}
  - {id: projects,   fixed: false, count: 3, bullets_per_project: 2, line_budget: 11}
  - {id: skills,     fixed: false, rows: [
      {id: row.programming, lines: 1, max_chars: 95},
      {id: row.methods,     lines: 2, max_chars: 195},
      {id: row.tools,       lines: 2, max_chars: 195},
      {id: row.spoken,      lines: 1, max_chars: 105, fixed: true}]}
```

### `experience.yaml` (FIXED — 3 entries, 3/3/1 bullets)

```yaml
- id: ophis
  org: Ophis Research
  role: Machine Learning Engineer | FinTech ML Developer
  employment_type: Contract
  start: 2025-11
  end: present
  bullets:
    - id: ophis.b1          # 165 chars → 2 lines. Variants must be 98–194 chars.
      text: "Built a swing-point classifier on 4 years of ETH 15-minute bars, fractal-labelled with nine online-safe features and validation-tuned thresholds (0.75/0.82 by max-F1)"
      tags: [python, machine_learning, time_series, classification, finance, feature_engineering]
      strength: strong
    - id: ophis.b2          # 89 chars → 1 line. Variants must be ≤ 97 chars.
      text: "Built a Monte Carlo pre-trade engine (GBM and bootstrap paths) scoring VaR, CVaR and Kelly"
      tags: [monte_carlo, simulation, probability, risk_modeling, quantitative, finance, python]
      strength: strong
    - id: ophis.b3          # 89 chars → 1 line
      text: "Replayed every simulated path under the live exit rules, keeping sim and execution in step"
      tags: [simulation, validation, testing, python]
      strength: medium

- id: freedomcode
  org: FreedomCode
  role: Technical Program Associate, CTO & CIO office
  start: 2024-01
  end: 2024-05
  bullets:
    - id: freedomcode.b1    # 152 chars → 2 lines
      text: "Worked directly with the CTO and CIO to map and streamline delivery workflows across cross-functional web and mobile teams, cutting project timelines ~20%"
      tags: [stakeholder_management, process_optimization, communication]
      strength: medium
    - id: freedomcode.b2    # 161 chars → 2 lines
      text: "Designed and implemented a structured data pipeline that replaced ad-hoc status reporting, halving redundant communication and freeing engineers for technical work"
      tags: [data_engineering, etl, process_optimization]
      strength: medium
    - id: freedomcode.b3    # 193 chars → 3 lines  ⚠ longest bullet on the page
      text: "Analysed product and Google Analytics data to locate UX and performance bottlenecks, presented findings to technical and non-technical stakeholders, and ran QA test protocols to validate the fixes"
      tags: [analytics, communication, stakeholder_management, testing]
      strength: medium

- id: beesurfing
  org: BeeSurfing
  role: Information Systems Developer
  start: 2024-01
  end: 2024-06
  bullets:
    - id: beesurfing.b1     # 166 chars → 2 lines
      text: "Built a cross-platform mobile application in Flutter and engineered a Firestore-based backend to support real-time booking, school availability, and group surf sessions"
      tags: [software, databases, mobile]
      strength: supporting
```

### `education.yaml` (FIXED — never varies)

```yaml
- id: edu.nova
  institution: Nova University of Lisbon | Information Management School
  degrees:
    - id: edu.msc
      degree: Master's Degree in Data Science and Advanced Analytics
      start: 2025-09
      end: 2027-07
      end_label: "expected Jul 2027"
      concentrations: "Big Data Analytics; Machine Learning; Deep Learning; Optimization"
      coursework: "Data Mining; Statistics for Data Science; Programming for Data Science; Text Mining; Storing & Retrieving Data"
    - id: edu.bsc
      degree: Bachelor's Degree in Information Systems and Technologies
      start: 2022-09
      end: 2025-01
      concentrations: "Data Engineering; Systems Analysis; App and Web Development"
      coursework: "Data Integration and Management; Database Systems; Big Data Analytics; Information Systems Design; Programming Languages; Business Process Modeling"
- id: edu.milano
  institution: University of Milano-Bicocca (Erasmus Exchange Program)
  start: 2025-01
  end: 2025-07
  note: "Courseworks in Deep Learning and Data Science"
- id: edu.lyceum
  institution: Dnipro Scientific Lyceum of Information Technologies
  start: 2019-09
  end: 2022-06
  note: "Graduated with Distinction (Top 5% of class) from a top-3 ranked academic lyceum in Ukraine"
```

### `projects.yaml` (VARIABLE — you currently have 3, you need 8–12)

```yaml
- id: proj.f1_warehouse
  name: "F1 Data Warehouse & ML Platform – PySpark"
  link_label: GitHub
  line_cost: 4                       # title 1 + b1 2 lines + b2 1 line
  themes: [data_engineering, dimensional_modelling, applied_ml]
  bullets:
    - id: proj.f1_warehouse.b1       # 147 chars → 2 lines  ⚠ consider trimming to ≤95
      text: "Built a medallion ETL pipeline over 74 years of F1 data across 14 tables, with explicit schemas, partitioned Parquet and referential-integrity checks"
      tags: [data_engineering, etl, pyspark, parquet, data_quality, databases]
    - id: proj.f1_warehouse.b2       # 84 chars → 1 line ✅
      text: "Modelled a star-schema gold layer; Spark MLlib scored ROC-AUC 0.878 and cut RMSE 73%"
      tags: [dimensional_modelling, machine_learning, pyspark, model_evaluation]
      metrics: [m.f1.roc_auc, m.f1.rmse]

- id: proj.covid_mlops
  name: "COVID-19 Severity Prediction – End-to-End MLOps Pipeline"
  link_label: GitHub
  line_cost: 3                       # ✅ both bullets 1 line — the ideal shape
  themes: [mlops, pipelines, healthcare]
  bullets:
    - id: proj.covid_mlops.b1        # 85 chars → 1 line ✅
      text: "Co-built a ten-stage Kedro pipeline from ingestion and data-quality checks to serving"
      tags: [mlops, kedro, etl, data_quality, pipelines]
    - id: proj.covid_mlops.b2        # 84 chars → 1 line ✅
      text: "Containerised serving with FastAPI and MLflow, plus PSI/KS drift and API-parity tests"
      tags: [mlops, docker, fastapi, mlflow, monitoring, testing]

- id: proj.pitwall
  name: "PitWall – Lead Discovery & Scoring Pipeline"
  link_label: "(walkthrough on request)"
  line_cost: 4
  themes: [pipelines, applied_ml, evaluation]
  bullets:
    - id: proj.pitwall.b1            # 86 chars → 1 line ✅
      text: "Built a seven-stage resumable pipeline over SQLite with cached multi-provider fallback"
      tags: [pipelines, databases, python, llm, system_design]
    - id: proj.pitwall.b2            # 158 chars → 2 lines
      text: "Chose recall over precision on a 60-case labelled set (0.62 → 0.94 for 0.67 → 0.61) because a missed lead costs more than a wasted review; 260 tests, 90% coverage"
      tags: [model_evaluation, testing, machine_learning, decision_making]
      metrics: [m.pitwall.recall, m.pitwall.coverage]

# ── ADD 5–9 MORE HERE ──
# Candidates from our earlier conversation that are not yet on the CV:
#   the WikiArt CNN classification project
#   the Airbnb database / query-optimisation project
#   your thesis work (race strategy under disruption)
# Each: exactly 2 bullets, each ≤ 95 characters, 3–6 tags, 1–3 themes.
```

**Current total if all three are selected:** 4 + 3 + 4 = **11 lines — exactly at the budget.** There is zero slack today. Trimming `proj.f1_warehouse.b1` and `proj.pitwall.b2` to ≤ 95 characters each would drop the trio to 9 lines and give the selector real freedom.

### `skills.yaml` (VARIABLE — rows 1–3 are packed per job)

Your current CV prints these. The master list must be **much longer** than this, or every CV prints the same skills.

```yaml
rows:
  row.programming:            # 1 line, ≤ 95 chars. Currently 42 chars — lots of room.
    pinned: [Python, SQL]
    master: [Python, SQL, R, JavaScript, TypeScript, C#]
  row.methods:                # 2 lines, ≤ 195 chars. Currently ~170.
    pinned: [Machine Learning]
    master: [Data Pipelines & ETL, Data Quality & Validation, Dimensional Modelling,
             Machine Learning, Generative AI & LLMs, Forecasting, Statistical Analysis,
             Predictive Modeling]
  row.tools:                  # 2 lines, ≤ 195 chars. Currently ~185 — nearly full.
    pinned: [pandas, scikit-learn]
    master: [pandas, NumPy, scikit-learn, PySpark, Spark SQL, Parquet, Kedro, MLflow,
             Docker, FastAPI, Streamlit, pytest, LangChain, Azure OpenAI, GitHub Actions,
             PyTorch, MongoDB, Power BI]
  row.spoken:                 # FIXED, 1 line, copied verbatim
    fixed_text: "English(fluent - MSc taught in English), Ukrainian(native), Russian(native), Portuguese(B1), Italian(A2)"
```

**Row 3 is the one that matters most.** It currently holds 19 tools in ~185 of 195 characters — effectively full, so almost nothing can be swapped in. Grow the master list to 30+ tools and the packer will genuinely tailor it: `PySpark, Spark SQL, Parquet, Kedro` for a data-engineering role, `PyTorch, MLflow, Docker, FastAPI` for an ML-engineering one.

Then add the confidence metadata from §8 to each entry — the master list above is only the printable names.

### What your CV does *not* have

**No summary or profile paragraph.** Your layout goes straight from contact to experience. So `summaries.yaml` (§10) is **not used by the CV** — keep it only as raw material for the cover letter's opening, or skip it entirely. Do not add a summary section; that would change the frame.

### Three things to confirm before the developer starts

All three of my earlier questions are **resolved from the Canva source**:

- **Section order** — `CONTACT → SKILLS → EXPERIENCE → PROJECTS → EDUCATION`, labels in a left gutter.
- **Skill row labels** — there are none; the four rows are unlabelled bullet points.
- **Source file** — Canva.

Three new ones the Canva source raised:

1. **Font licence.** The name and title use **Machina Ultrabold**, a commercial typeface. Confirm your licence covers exporting generated documents, or accept a substitute chosen once at the template level.
2. **Project bullet styling is inconsistent.** F1's bullets are upright; COVID-19's and PitWall's are italic. Since projects are now selected dynamically, per-project styling cannot survive — pick one and it applies to all three.
3. **ATS risk from the text-frame layout.** Your PDF's text extracts in non-linear order because the design is assembled from separate Canva text boxes. Position-aware parsers cope; weaker ones can attach the wrong date to the wrong entry. The build plan therefore produces a **second, single-column DOCX** alongside the designed PDF — same content, plainer structure — for portals that parse badly. You submit the PDF by default.
