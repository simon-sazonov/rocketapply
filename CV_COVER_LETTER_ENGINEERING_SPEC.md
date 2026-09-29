# Tailored CV & Cover Letter Engine — Engineering Specification

**Audience:** the developer(s) implementing this system.
**Status:** implementation-ready design spec. v1.0
**Scope:** everything downstream of "a job has been selected from the CSV". Job discovery is already solved and is *out of scope*.

---

## 0. TL;DR for the impatient

Build a **90% deterministic / 10% LLM** pipeline.

> **The LLM decides what is *relevant*. The program guarantees what is *true*.**

The LLM never writes a CV. **The CV layout is a frozen frame** with exactly three variable slots — which 3 projects appear, which skills fill each row, and which pre-approved phrasing an experience bullet uses. The LLM emits a **slot configuration** of IDs; a deterministic renderer with an arithmetic fit guarantee turns it into DOCX/PDF. Everything else on the page is a constant.

The LLM does write the cover letter prose, but only after a separate planning step, and only from a whitelist of verified facts, and its output is then machine-checked against that whitelist before it is allowed to reach disk.

Anything the model tries to state that is not traceable to a profile record is a **build failure**, not a stylistic preference.

---

## 1. Goals and non-goals

### 1.1 Goals

| # | Goal | Measured by |
|---|------|-------------|
| G1 | Zero hallucinated facts in output | Fact-checker + manual audit on 30 jobs: 0 unsupported claims |
| G2 | Job-specific emphasis, same underlying truth | Two different jobs produce materially different CVs from one profile |
| G3 | One-page CV, deterministic layout | Renderer enforces page budget before writing the file |
| G4 | Cover letter needing ≤ 20–30% manual editing | Human rating rubric, tracked per run |
| G5 | Reproducible | Same job + same profile + same seed ⇒ same output, or a recorded diff |
| G6 | Cheap | ≤ ~6k tokens of Groq quota per application |

### 1.2 Non-goals

- Not a web app (v1 is a local CLI + optional thin TUI/Streamlit later).
- Not a job scraper. The CSV already exists.
- Not an auto-submitter. Human reviews and sends every application.
- Not a general-purpose "resume AI". It serves exactly one profile.

### 1.3 Hard constraints (non-negotiable)

1. **No new factual content may originate from the model.** Ever.
2. **No numbers may be invented, rounded, or re-derived.** Metrics are copied verbatim from the profile or omitted.
3. **No employer, tool, framework, cloud provider, or certification may appear in output unless present in the profile.**
4. **No seniority inflation.** "Led", "owned", "managed", "architected", "in production" are gated vocabulary (see §7.4).
5. **The CV layout is frozen.** Section order (`CONTACT → SKILLS → EXPERIENCE → PROJECTS → EDUCATION`), section contents, the experience block and the education block are constants. Only three slots vary (§8). The model has no field in its output schema with which to change anything else.
6. **Every generated artifact ships with a machine-readable provenance file** listing which profile IDs were used.

---

## 2. Architecture

```text
                 jobs.csv
                    │
             [ select job ]
                    │
                    ▼
        ┌───────────────────────────┐
        │ STAGE 1  Job Analyzer     │  LLM (Groq, strict JSON)
        │ role / skills / duties    │
        └────────────┬──────────────┘
                     ▼
        ┌───────────────────────────┐
        │ STAGE 2  Matching Engine  │  DETERMINISTIC (+ optional local LLM
        │ requirement → evidence    │   only for fuzzy synonym resolution)
        └────────────┬──────────────┘
                     ▼
        ┌───────────────────────────┐
        │ STAGE 3  Truth Layer      │  DETERMINISTIC
        │ allowed / forbidden facts │
        └────────────┬──────────────┘
                     │
         ┌───────────┴────────────┐
         ▼                        ▼
┌──────────────────┐   ┌────────────────────────┐
│ STAGE 4a         │   │ STAGE 4b               │
│ Slot Selector    │   │ Narrative Planner      │
│ LLM → cv_config  │   │ LLM → narrative_plan   │
│ (3 slots only)   │   │                        │
└────────┬─────────┘   └───────────┬────────────┘
         ▼                         ▼
┌──────────────────┐   ┌────────────────────────┐
│ STAGE 5a         │   │ STAGE 5b               │
│ CV Renderer      │   │ Letter Writer (LLM)    │
│ FROZEN FRAME     │   │ prose from plan only   │
│ + fit solver     │   │                        │
└────────┬─────────┘   └───────────┬────────────┘
         │                         ▼
         │             ┌────────────────────────┐
         │             │ STAGE 6  Fact Checker  │ local LLM + regex gates
         │             │ PASS / FAIL per claim  │
         │             └───────────┬────────────┘
         │                    fail │ ◄── retry ≤ 2, then hard stop
         ▼                         ▼ pass
        ┌───────────────────────────┐
        │ STAGE 7  Packager         │  DOCX → PDF, naming, provenance
        └────────────┬──────────────┘
                     ▼
        output/<company>_<role>/
```

Every stage is a **pure function** with typed input and typed output, serialized to `run/` as JSON. This makes each stage independently testable, cacheable, and replayable without re-calling the model.

---

## 3. Repository layout

```text
job_application_ai/
├── profile/                      # THE SOURCE OF TRUTH — hand-authored, version-controlled
│   ├── personal.yaml
│   ├── education.yaml
│   ├── experience.yaml
│   ├── projects.yaml
│   ├── skills.yaml
│   ├── evidence.yaml             # narrative blocks for cover letters
│   ├── summaries.yaml            # 3–6 pre-written CV summary variants
│   ├── style.yaml                # voice samples + banned phrases
│   ├── layout.yaml               # THE FROZEN CV FRAME — slot quotas, line budgets
│   └── forbidden.yaml            # explicit do-not-claim list
│
├── schema/                       # JSON Schemas — validated on load AND on LLM output
│   ├── profile.schema.json
│   ├── job_analysis.schema.json
│   ├── match_result.schema.json
│   ├── cv_config.schema.json
│   ├── narrative_plan.schema.json
│   └── fact_check.schema.json
│
├── prompts/                      # every prompt is a versioned file, never an f-string in code
│   ├── 01_job_analysis.md
│   ├── 02_synonym_resolve.md
│   ├── 04a_cv_select.md
│   ├── 04b_narrative_plan.md
│   ├── 05b_letter_write.md
│   └── 06_fact_check.md
│
├── app/
│   ├── cli.py
│   ├── config.py
│   ├── llm.py                    # existing provider chain, extended with task routing
│   ├── models.py                 # pydantic models mirroring schema/
│   ├── profile_loader.py
│   ├── job_analyzer.py           # stage 1
│   ├── matcher.py                # stage 2
│   ├── truth_layer.py            # stage 3
│   ├── cv_selector.py            # stage 4a
│   ├── narrative_planner.py      # stage 4b
│   ├── layout.py                 # frame loader, line-cost model, fit solver
│   ├── cv_renderer.py            # stage 5a
│   ├── letter_writer.py          # stage 5b
│   ├── fact_checker.py           # stage 6
│   ├── packager.py               # stage 7
│   └── taxonomy.py               # canonical skill vocabulary + alias map
│
├── templates/
│   ├── cv.docx.j2      (or docxtpl template / direct python-docx builder)
│   └── cover_letter.docx
│
├── jobs/
│   └── *.csv
│
├── run/                          # per-run intermediate artifacts (gitignored)
│   └── 2026-09-23_bmw_graduate-ds/
│       ├── 01_job_analysis.json
│       ├── 02_match.json
│       ├── 03_truth.json
│       ├── 04a_cv_config.json
│       ├── 04b_plan.json
│       ├── 05b_letter_draft_v1.txt
│       ├── 06_factcheck.json
│       └── trace.jsonl            # every LLM call: model, prompt hash, tokens, latency
│
├── output/
│   └── BMW_Graduate_Data_Scientist/
│       ├── Simon_Sazonov_BMW_Graduate_Data_Scientist_CV.pdf
│       ├── Simon_Sazonov_BMW_Graduate_Data_Scientist_Cover_Letter.pdf
│       ├── *.docx
│       └── provenance.json
│
└── tests/
    ├── fixtures/jobs/*.txt
    ├── test_matcher.py
    ├── test_truth_layer.py
    ├── test_renderer_budget.py
    └── test_fact_checker_golden.py
```

**Rule:** `profile/` is the only place facts live. No fact may be typed into a prompt file, a template, or Python source.

---

## 4. Data model

All profile files are YAML (human-editable), loaded into pydantic models, validated against JSON Schema at startup. **Fail fast and loudly** — a profile that does not validate must abort the run with a precise error naming the file, the record ID, and the field.

### 4.1 Identifier rules

- Every bullet, project, experience, skill and evidence block has a **stable, unique, human-readable ID**: `ophis.b2`, `proj.wikiart.b1`, `skill.python`, `ev.race_strategy`.
- IDs are **immutable**. Renaming an ID invalidates every stored run. If a bullet is rewritten materially, retire the old ID and create a new one.
- ID regex: `^[a-z0-9_]+(\.[a-z0-9_]+)*$`

### 4.2 `experience.yaml` / `projects.yaml` — the bullet record

```yaml
- id: ophis
  type: experience            # experience | project | education
  org: Ophis Research
  role: Data Science Intern   # EXACT title as it will be printed
  location: Remote
  start: 2025-02
  end: 2025-08                # or "present"
  context: >
    Quantitative research team building ML-driven trading research tooling.
  bullets:
    - id: ophis.b1
      text: >
        Developed a Monte Carlo risk model estimating probabilities of
        stop-loss and take-profit outcomes across simulated price paths.
      tags: [python, monte_carlo, probability, risk_modeling, simulation,
             quantitative, machine_learning, finance]
      metrics: []             # see §4.4
      strength: strong        # strong | medium | supporting
      length_chars: 118       # computed at load time, used by page budget
      variants:               # OPTIONAL pre-approved rephrasings (§7.3)
        - id: ophis.b1.v2
          text: >
            Built a Monte Carlo simulation to quantify stop-loss / take-profit
            outcome probabilities under stochastic price dynamics.
      use_for: [data_science, quantitative, machine_learning, risk]
      never_use_for: []
```

**Field semantics**

| Field | Purpose |
|-------|---------|
| `text` | The canonical, truthful sentence. Printed verbatim by default. |
| `tags` | Canonical vocabulary only (§4.6). This is how matching works. |
| `strength` | Ranking hint. A `strong` bullet outranks a `medium` one at equal match score. |
| `variants` | **Pre-approved** alternate phrasings written by the human, not the model. Lets the CV read differently per job with zero hallucination risk. |
| `use_for` / `never_use_for` | Coarse role-family gating. Prevents e.g. a finance-heavy bullet leading an automotive application. |

### 4.3 `skills.yaml` — with a confidence ladder

```yaml
- id: skill.python
  name: Python
  category: programming        # programming | ml | data | stats | tools | domain | language
  confidence: high             # high | medium | low | none
  evidence: [ophis.b1, ophis.b3, proj.wikiart.b1, edu.msc]
  years: 4
  aliases: [python3, py]
  printable: true              # may appear in the CV skills block

- id: skill.aws
  name: AWS
  category: tools
  confidence: none
  evidence: []
  status: NOT_SUPPORTED        # renderer and fact checker both treat this as forbidden
  printable: false
```

**Confidence ladder — this is the heart of the truth layer.**

| Level | Meaning | Allowed in CV skills? | Allowed as a letter claim? |
|-------|---------|----------------------|---------------------------|
| `high` | ≥ 2 independent evidence records, used substantively | yes | yes, may be a headline claim |
| `medium` | 1 evidence record, or used in a limited scope | yes | yes, but only phrased as the specific project |
| `low` | coursework / exposure only | only if the job explicitly requires it, and only in a `Familiar with` sub-line | never as a claim of experience |
| `none` | no evidence | **never** | **never**; auto-added to the forbidden list |

A skill at `low` **must** carry `low_phrasing:` — the exact wording the human approves, e.g. `"coursework exposure to Docker"`. The model may not paraphrase it.

### 4.4 Metrics — the strictest object in the system

```yaml
metrics:
  - id: m.airbnb.query_time
    claim: "reduced query execution time from 15,832 ms to 63 ms"
    raw_before: 15832
    raw_after: 63
    unit: ms
    method: "indexed B-tree on composite key; measured with EXPLAIN ANALYZE, n=20 runs"
    verifiable: true
```

Rules:
1. A number may appear in output **only** if it appears in a `metrics` record.
2. The renderer copies `claim` **verbatim**. No rounding ("~250x faster" is a *new* number unless that string is itself in the record).
3. The fact checker extracts every numeric token from generated prose and requires an exact match against the metric corpus. Unmatched number ⇒ FAIL.
4. If you want "251× faster" to be available, the human adds it as a second metric record. Software never computes it.

### 4.5 `evidence.yaml` — cover-letter narrative blocks

These are **facts plus framing**, deliberately *not* finished prose.

```yaml
- id: ev.ophis_ml
  title: ML systems for quantitative research
  source_refs: [ophis.b1, ophis.b2, ophis.b3]
  facts:
    - developed machine learning systems for quantitative research
    - built probabilistic and Monte Carlo risk models
    - worked in Python on financial time-series data
  skills: [python, machine_learning, probability, risk_modeling, time_series]
  themes: [applied_ml, decision_under_uncertainty, quantitative_rigor]
  use_for: [data_science, machine_learning, quantitative, applied_science]
  strength: strong
  one_line_hook: >
    Turning probabilistic models into decisions that have to hold up
    against real market outcomes.
```

`facts` are the only assertions the writer model may make about this block. `one_line_hook` is human-written thematic framing the model may use *as an idea*, not necessarily verbatim.

### 4.6 Canonical tag taxonomy (`taxonomy.py`)

A closed vocabulary. Every tag in the profile **must** exist in it, or profile validation fails.

```python
CANONICAL = {
  "python", "sql", "r", "java", "bash",
  "machine_learning", "deep_learning", "computer_vision", "nlp",
  "time_series", "reinforcement_learning", "optimization", "simulation",
  "monte_carlo", "probability", "statistics", "bayesian", "experimentation",
  "data_engineering", "databases", "etl", "performance", "visualization",
  "pytorch", "tensorflow", "sklearn", "pandas", "numpy", "docker", "git",
  "cloud", "aws", "azure", "gcp", "mlops", "deployment",
  "finance", "automotive", "motorsport", "healthcare", "retail",
  "communication", "stakeholder_management", "research",
}

ALIASES = {
  "ml": "machine_learning",
  "dl": "deep_learning",
  "cv": "computer_vision",          # careful: disambiguate from "curriculum vitae"
  "postgres": "sql", "mysql": "sql", "postgresql": "sql",
  "scikit-learn": "sklearn", "sci-kit learn": "sklearn",
  "amazon web services": "aws",
  "neural networks": "deep_learning",
  "predictive modelling": "machine_learning",
  "statistical modeling": "statistics",
  # ... maintained as a data file, grown from observed job postings
}
```

`normalize(term) -> canonical | None`. Unknown terms from job postings are **not** dropped silently — they are recorded in `match.unmatched_terms` and logged to `run/unknown_terms.log` so the taxonomy can be grown deliberately.

---

## 5. Stage 1 — Job Analyzer

**Model:** Groq `openai/gpt-oss-20b`, strict schema JSON, `reasoning_effort=low`, `temperature=0`.

**Input:** `{title, company, location, description_text, url}`

**Output schema (`job_analysis.schema.json`), all fields required:**

```json
{
  "role_family": "DATA_SCIENCE | MACHINE_LEARNING | DATA_ENGINEERING | ANALYTICS | RESEARCH | QUANTITATIVE | SOFTWARE | OTHER",
  "seniority": "INTERN | ENTRY_LEVEL | JUNIOR | MID | SENIOR | UNKNOWN",
  "required_skills":  [{"term": "Python", "evidence_quote": "strong Python skills"}],
  "preferred_skills": [{"term": "PyTorch", "evidence_quote": "experience with PyTorch is a plus"}],
  "responsibilities": ["Build and evaluate ML models", "..."],
  "domain": ["automotive", "manufacturing"],
  "keywords": ["predictive maintenance", "time series"],
  "language": "en | de | pt | ...",
  "company_signals": ["mentions cross-functional collaboration", "R&D-heavy"],
  "hard_gates": ["requires German C1", "requires EU work permit", "requires 3+ years"]
}
```

### Design notes

1. **`evidence_quote` is mandatory for every extracted skill.** It is a short verbatim span from the posting. This is cheap anti-hallucination: the program verifies the quote is an actual substring (normalized whitespace, case-insensitive) of the description. If not ⇒ drop that skill and log it. A 20B model *will* occasionally invent a requirement; this catches it for free.
2. **`hard_gates`** feed a pre-flight warning: if the job demands 3+ years and the profile has none, the CLI prints a warning before spending tokens. Do not auto-abort — the human decides.
3. Truncate description to ~6000 characters, keeping the head and the "requirements"/"qualifications" section if detected by heading regex. Log when truncation occurs.
4. If the posting language is not English, still extract into English canonical terms, but record `language` — it drives letter language selection (§9.1).
5. Cache by `sha256(description_text)` so re-running a job costs nothing.

**Fallback ladder:** strict JSON → `json_object` mode → repair prompt → local Qwen → hard fail with the raw text preserved for manual entry. Never silently proceed with a half-parsed analysis.

---

## 6. Stage 2 — Matching Engine (deterministic)

This stage **should not need the LLM at all** in the common case.

```python
def match(job: JobAnalysis, profile: Profile) -> MatchResult:
    for req in job.required_skills + job.preferred_skills:
        canon = normalize(req.term)          # alias table first
        if canon is None:
            canon = llm_synonym_resolve(req.term)   # local Qwen, cached forever
        skill = profile.skills.by_canonical(canon)
        ...
```

**Output:**

```json
{
  "matched":  [{"requirement": "Python", "skill_id": "skill.python",
                "confidence": "high", "evidence_ids": ["ophis.b1", "proj.wikiart.b1"]}],
  "partial":  [{"requirement": "Cloud", "skill_id": "skill.cloud",
                "confidence": "low", "approved_phrasing": "coursework exposure to cloud deployment"}],
  "missing":  [{"requirement": "AWS", "criticality": "preferred"}],
  "unmatched_terms": ["SAP HANA"],
  "coverage": {"required": 0.83, "preferred": 0.40, "overall_score": 0.71}
}
```

### Scoring (tunable, keep it simple and explainable)

```
bullet_score(b, job) =
      3.0 * |tags(b) ∩ required_canonical|
    + 1.5 * |tags(b) ∩ preferred_canonical|
    + 1.0 * |tags(b) ∩ keyword_canonical|
    + 0.8 * strength_weight(b)          # strong=1.0, medium=0.6, supporting=0.3
    + 0.5 * recency_weight(b)           # linear decay over 4 years
    - 5.0 * (1 if job.role_family in b.never_use_for else 0)
    + 1.0 * (1 if job.role_family in b.use_for else 0)
```

Do **not** use embeddings in v1. The tag intersection is explainable, debuggable, and free. Add embeddings only if you measure that alias maintenance is the bottleneck — and if you do, use them to *suggest* alias entries for human approval, not to match at runtime.

`coverage.overall_score` is surfaced to the user as a **go/no-go signal**: below ~0.45, the honest advice is "this job is a stretch; here is what is missing", not "generate an application anyway".

---

## 7. Stage 3 — The Truth Layer (deterministic, the most important module)

Produces a single object consumed by every downstream stage and by the fact checker.

```json
{
  "allowed_facts": [
    {"id": "ophis.b1", "text": "Developed a Monte Carlo risk model ..."},
    {"id": "m.airbnb.query_time", "text": "reduced query execution time from 15,832 ms to 63 ms"}
  ],
  "allowed_skills": ["Python", "SQL", "Machine Learning", "Statistics", "PyTorch"],
  "allowed_orgs": ["Ophis Research", "NOVA IMS"],
  "allowed_numbers": ["15832", "63", "4"],
  "forbidden_claims": [
    "AWS", "Azure", "GCP", "Kubernetes", "production deployment",
    "team leadership", "managed a team", "years of professional experience in X"
  ],
  "gated_verbs": ["led", "owned", "managed", "architected", "spearheaded", "directed"],
  "approved_low_phrasings": {"cloud": "coursework exposure to cloud deployment"}
}
```

### 7.1 Construction

- `allowed_*` = union of everything reachable from the profile.
- `forbidden_claims` = `profile/forbidden.yaml` ∪ every skill with `confidence: none` ∪ every `missing` requirement from Stage 2.

**That last clause is the key insight.** The moment the matcher finds the job wants AWS and Simon has none, AWS is *actively forbidden* for this run. The most dangerous hallucination is precisely the one that closes the gap the model just noticed.

### 7.2 Level 1 — Selection (always allowed)

Choosing which 3 projects appear, which bullets within them, and which skills fill each row. This is the overwhelming majority of tailoring and carries **zero** risk.

**Not permitted, even though it is "only selection":** reordering sections, adding or removing an experience entry, changing a bullet count, touching education. The frozen frame (§8) removes these from the model's reach entirely — they are absent from its output schema.

### 7.3 Level 2 — Rephrasing (allowed, tightly bounded)

Permitted only via **pre-approved `variants`** authored by the human, OR via a constrained rewrite that must pass:

- no new named entity (NER diff against the original must be empty)
- no new number
- no new gated verb
- cosine/Jaccard token overlap with the source ≥ 0.6
- length within ±20% of the source
- **identical rendered line cost** (§8.4) — a rephrasing that changes how many lines the bullet occupies breaks the frozen frame and is rejected at profile-validation time

If a constrained rewrite fails any check, **fall back to the canonical `text`**. Never ship the rewrite and never retry more than once. In v1, prefer shipping only human-authored variants and defer free rewriting to v1.1.

### 7.4 Level 3 — New factual content (forbidden)

Enforced by the fact checker (§10) and by the renderer refusing any string not traceable to an ID.

**Gated verbs** deserve special mention: a 20B model reaches for "led the development of" as a stylistic tic. The claim ledger must flag any gated verb whose subject is not backed by a bullet that itself contains that verb.

---

## 8. The CV — a FROZEN FRAME with three variable slots

> **This is the most important section of the spec.** The CV layout is not a suggestion, a starting point, or something the model may reorganise. It is a fixed geometric frame, calibrated once against Simon's existing CV, and it never changes. The model fills three slots inside it and nothing else.

### 8.1 The frame (calibrated from the Canva source + exported PDF)

A4, 595.5 × 842 pt, one page. **Two-column structure:** a narrow left gutter holds the section labels (`CONTACT`, `SKILLS`, `EXPERIENCE`, `PROJECTS`, `EDUCATION` — small caps, right-aligned), and a wide right column holds all content. Fonts: `Machina Ultrabold` (name/title), `Arimo Bold` (gutter labels), `DM Sans` Regular/Italic/Bold (body).

```text
┌──────────────┬──────────────────────────────────────────────────┐
│              │ Simon Sazonov                     Data Scientist │  FIXED
├──────────────┼──────────────────────────────────────────────────┤
│  CONTACT     │ Email / GitHub / LinkedIn        (3 lines)       │  FIXED
├──────────────┼──────────────────────────────────────────────────┤
│  SKILLS      │ • row 1  programming languages   (1 line)        │  ◄── SLOT B
│              │ • row 2  methods & techniques    (2 lines)       │      contents vary,
│              │ • row 3  tools & libraries       (2 lines)       │      row count fixed
│              │ • row 4  spoken languages        (1 line)        │      row 4 FIXED
├──────────────┼──────────────────────────────────────────────────┤
│  EXPERIENCE  │ Ophis Research – ML Eng | FinTech ML Developer   │  FIXED entries,
│              │   • • •                          (3 bullets)     │  FIXED count,
│              │ FreedomCode – Technical Program Associate        │  FIXED order.
│              │   • • •                          (3 bullets)     │  ── wording may swap
│              │ BeeSurfing – Information Systems Developer       │     for a LINE-NEUTRAL
│              │   •                              (1 bullet)      │     variant  ◄── SLOT C
├──────────────┼──────────────────────────────────────────────────┤
│  PROJECTS    │ <project 1>                      (2 bullets)     │  ◄── SLOT A
│              │ <project 2>                      (2 bullets)     │      exactly 3, chosen
│              │ <project 3>                      (2 bullets)     │      from the library
├──────────────┼──────────────────────────────────────────────────┤
│  EDUCATION   │ Nova Lisbon | IMS → MSc + BSc                    │  FIXED, ENTIRELY.
│              │ University of Milano-Bicocca (Erasmus)           │  Never varies.
│              │ Dnipro Scientific Lyceum of Information Tech.    │
└──────────────┴──────────────────────────────────────────────────┘
```

**Note the order:** `CONTACT → SKILLS → EXPERIENCE → PROJECTS → EDUCATION`. Skills sit near the top, education at the bottom. That is correct for a graduate profile — the skills block is the fastest thing a screener wants to confirm — and it is a constant.

### 8.1a Source-format finding — the Canva original is not a build target

The CV is authored in Canva. Two consequences the implementer must plan for:

1. **Canva cannot be driven programmatically here.** Its Autofill / Brand Template API requires a Canva Enterprise or Teams plan and a registered Connect app. Do not design around it. **Rebuild the template.**
2. **The design is assembled from independent text frames**, which is why the exported PDF's text extracts in non-linear order — skills, projects, education and experience come out interleaved rather than top-to-bottom. Parsers that use glyph positions cope fine; naive ones can associate a date with the wrong entry.

**Recommended build path:**

| Output | Technology | Purpose |
|--------|-----------|---------|
| **Primary PDF** | HTML + CSS → **WeasyPrint** | Exact visual match to the Canva design, CSS Grid for the gutter, deterministic pagination — and critically, the renderer reports *actual* rendered line counts and page count, upgrading §8.4 from an estimate to a measurement |
| **ATS variant DOCX** | `python-docx`, single column, gutter labels promoted to inline headings | For portals that demand DOCX or parse badly; visually plainer, structurally bulletproof |

Generate both on every run. Submit the PDF by default; keep the DOCX for portals that ask for it.

Fonts must be licensed and installed on the build machine. Machina is a commercial face — if the licence does not cover this use, substitute once at the template level and re-run `calibrate`, never per application.

### 8.2 What varies and what does not — the contract

| Region | Varies? | What may change | What may never change |
|--------|---------|-----------------|----------------------|
| Header, contact | **No** | nothing | everything |
| Section order | **No** | nothing | `CONTACT → SKILLS → EXPERIENCE → PROJECTS → EDUCATION` |
| Experience — entries | **No** | nothing | all 3 always appear, in this order |
| Experience — bullet count | **No** | nothing | Ophis 3, FreedomCode 3, BeeSurfing 1 |
| Experience — bullet *wording* | **Yes, narrowly** | swap a bullet for a pre-approved, **line-neutral** variant | the fact, the numbers, the line count |
| Education | **No** | nothing | everything |
| Projects — which 3 | **Yes** | choose 3 from the project library | there are always exactly 3 |
| Projects — bullets | **Yes** | 2 bullets per project, from that project's bullet pool | always exactly 2 per project |
| Skills — row structure | **No** | nothing | 4 unlabelled bulleted rows, fixed line counts (1/2/2/1) |
| Skills — row contents | **Yes** | which items appear, and their order | rows 1–3 draw from the master list; row 4 is fixed |

Three degrees of freedom. That is the whole system. Everything else is a constant.

### 8.3 `profile/layout.yaml` — the frame as data

The geometry lives in a config file, not in code, so it can be recalibrated without a rewrite.

```yaml
page:
  size: A4
  max_pages: 1
  layout: two_column          # narrow gutter for labels, wide column for content
  # Calibrated by measuring wrapped lines in the reference PDF.
  chars_per_line_bullet: 97
  chars_per_line_fullwidth: 105
  total_body_lines: 57

section_order: [contact, skills, experience, projects, education]   # NEVER changes

sections:
  - id: contact
    fixed: true
    line_budget: 3
  - id: skills
    fixed: false
    style: bulleted           # each row is a bullet; rows are UNLABELLED
    rows:
      - {id: row.programming, lines: 1, max_chars: 95,  source: category.programming, fixed: false}
      - {id: row.methods,     lines: 2, max_chars: 195, source: category.methods,     fixed: false}
      - {id: row.tools,       lines: 2, max_chars: 195, source: category.tools,       fixed: false}
      - {id: row.spoken,      lines: 1, max_chars: 105, source: category.spoken,      fixed: true}
  - id: experience
    fixed: true
    line_budget: 17
    entries:
      - {id: ophis,       bullets: 3, order: 1}
      - {id: freedomcode, bullets: 3, order: 2}
      - {id: beesurfing,  bullets: 1, order: 3}
  - id: projects
    fixed: false
    count: 3                     # EXACTLY three, never two, never four
    bullets_per_project: 2       # EXACTLY two
    line_budget: 11              # excluding the gutter label
  - id: education
    fixed: true
    line_budget: 14
```

**Styling inconsistency to resolve before templating:** in the current design the F1 project bullets render in DM Sans Regular while the COVID-19 and PitWall bullets render in DM Sans Italic. Since projects are now selected dynamically, per-project styling cannot survive — choose one style for all project bullets and set it in the template.

**`line_budget` values are derived from the reference PDF and must be re-measured if the template is ever touched.** A `calibrate` CLI command should re-derive them from a rendered reference and fail loudly if they drift.

### 8.4 The line-cost model — the invariant that makes fitting a guarantee

Every renderable piece of text declares its cost in rendered lines:

```python
def line_cost(text: str, chars_per_line: int) -> int:
    return max(1, math.ceil(len(text.strip()) / chars_per_line))
```

Computed at **profile load time** and stored on every bullet. From this follows the single rule that replaces the old greedy page-budget search:

> **A substitution is legal only if it is line-neutral or the slot has proven slack.**

- An experience-bullet variant must satisfy `line_cost(variant) == line_cost(canonical)`. A variant that fails this is rejected at profile-validation time, not at render time.
- A project's `line_cost` is `1 (title) + line_cost(b1) + line_cost(b2)`, so it is 3, 4 or 5.
- The three selected projects must satisfy `Σ line_cost ≤ 11`.

Because every cost is known before rendering, **overflow is impossible by construction**. The PDF page-count assertion (§8.8) remains as a belt-and-braces check, not as the primary mechanism.

### 8.5 SLOT A — project selection (deterministic, exact)

The project library should hold **8–12 projects**. Exactly 3 are printed.

This is a tiny 0/1 knapsack with a cardinality constraint. With ~12 candidates there are `C(12,3) = 220` combinations — **solve it exactly by brute force**. No heuristics, no greedy approximation, no LLM arithmetic.

```python
def select_projects(library, job, quota=11, k=3):
    scored = {p.id: relevance(p, job) for p in library}
    best = None
    for combo in itertools.combinations(library, k):
        cost = sum(p.line_cost for p in combo)
        if cost > quota:
            continue
        score = sum(scored[p.id] for p in combo)
        score += 0.4 * diversity_bonus(combo)   # reward distinct themes
        score -= 2.0 * sum(1 for p in combo if job.role_family in p.never_use_for)
        if best is None or score > best[0]:
            best = (score, combo, cost)
    if best is None:
        raise LayoutInfeasible("no 3-project combination fits the slot")
    return best
```

`diversity_bonus` prevents three near-identical pipeline projects crowding out a modelling project: count distinct `themes` across the combo.

**Bullet choice within a project:** each project declares a pool of 3–5 bullets; pick the 2 highest-scoring whose combined `line_cost` keeps the project at or under its declared cost. Same brute force, `C(5,2) = 10` combinations.

**Ordering of the chosen 3:** by relevance score, descending. The strongest match reads first.

**The LLM's role here is advisory only.** It may return a ranked preference over projects; the solver treats that as one term in `relevance()` and still enforces the constraint. If the model's preferred trio does not fit, the solver picks the best feasible trio and records the override in `provenance.json`. The model never gets to produce an infeasible page.

### 8.6 SLOT B — skills row packing (deterministic)

Each of rows 1–3 is a comma-joined list packed to a hard character budget.

```python
def pack_row(candidates, job, max_chars, pinned):
    # 1. pinned items always first, in declared order
    chosen = list(pinned)
    # 2. rank the rest: job-required > job-preferred > confidence > declared order
    ranked = sorted(candidates, key=lambda s: (
        -3 * (s.canonical in job.required),
        -2 * (s.canonical in job.preferred),
        -CONF_WEIGHT[s.confidence],
        s.order,
    ))
    for s in ranked:
        if s in chosen or not s.printable:
            continue
        trial = ", ".join(x.name for x in chosen + [s])
        if len(trial) <= max_chars:
            chosen.append(s)
    return ", ".join(x.name for x in chosen)
```

Rules that fall out of this:

1. **`printable: false` and `confidence: none` skills can never enter a row.** The packer never sees them.
2. **Pinned items** (`pin: true` in `skills.yaml`) always appear — Python and SQL should be pinned in row 1 regardless of the posting.
3. Rows are **packed to the budget, not to a fixed item count**. Short skill names mean more skills fit; that is fine and intended.
4. **Row 4 is fixed** and copied verbatim. Spoken languages do not vary by job.
5. A row must never render empty. If packing yields fewer than 3 items, fall back to the declared default set for that row (`default_items` in `skills.yaml`).

### 8.7 The CV configuration — what the model actually emits

Note what is **gone** compared to a general-purpose resume generator: no `sections_order`, no `experience` selection, no `education` selection, no summary variant. The model cannot touch what it cannot address.

```json
{
  "projects": [
    {"id": "proj.f1_warehouse", "bullets": ["proj.f1_warehouse.b1", "proj.f1_warehouse.b3"]},
    {"id": "proj.covid_mlops",  "bullets": ["proj.covid_mlops.b1",  "proj.covid_mlops.b2"]},
    {"id": "proj.pitwall",      "bullets": ["proj.pitwall.b1",      "proj.pitwall.b2"]}
  ],
  "skills": {
    "row.programming": ["Python", "SQL", "R", "TypeScript"],
    "row.methods":     ["Machine Learning", "Forecasting", "Statistical Analysis", "Data Pipelines & ETL"],
    "row.tools":       ["pandas", "scikit-learn", "PySpark", "MLflow", "Docker", "PyTorch"]
  },
  "experience_variants": {
    "ophis.b1": "ophis.b1.v2",
    "freedomcode.b3": null
  },
  "rationale": {
    "proj.f1_warehouse": "posting emphasises Spark and dimensional modelling",
    "ophis.b1": "variant v2 foregrounds time-series over trading vocabulary"
  }
}
```

**Validation before rendering — every check aborts rather than degrades:**

| Check | On failure |
|-------|-----------|
| exactly 3 projects, all IDs exist | reject, retry once, then deterministic fallback |
| exactly 2 bullets per project, all IDs belong to that project | same |
| `Σ project line_cost ≤ 11` | reject; solver picks a feasible trio |
| every skill string is in `truth.allowed_skills` **and** belongs to that row's category | drop the item, log it |
| each packed row ≤ `max_chars` | re-pack deterministically |
| every `experience_variants` value is a declared variant of that exact bullet | reject the variant, use canonical |
| `line_cost(variant) == line_cost(canonical)` | reject the variant, use canonical |

`rationale` is never printed. It goes to `provenance.json` and it is the first thing you read when a CV comes out odd.

**Always implement the deterministic fallback:** top-3 projects by score subject to the quota, rows packed by rank, no variants. ~40 lines. It means a Groq outage produces a good CV, not no CV.

### 8.8 Rendering and the fit guarantee

Pure `python-docx` / `docxtpl` over the frozen template. No model involvement.

```
1. Load layout.yaml; assert every fixed section's content matches the profile exactly.
2. Render header, contact, experience, education      → constant
3. Render the 3 selected projects                     → slot A
4. Render the 4 skill rows                            → slot B
5. Sum declared line costs; assert total ≤ total_body_lines
6. Write DOCX → convert to PDF (LibreOffice headless)
7. Assert pypdf page_count == 1     ← if this ever fires, the calibration is wrong;
                                      log loudly, drop the lowest-scoring project
                                      bullet, re-render, and flag for recalibration
```

Step 7 firing is a **bug report**, not normal operation. In a correctly calibrated system it never triggers, because step 5 already proved the page fits. Treat any occurrence as a signal to re-run `calibrate`.

### 8.8a The ATS variant — formatting rules

The designed PDF is the primary submission. The DOCX variant exists because the Canva design is built from independent text frames (§8.1a) and some portals parse that badly. It carries identical content from identical IDs; only presentation differs.

| Do | Don't |
|----|-------|
| Single column, linear reading order, gutter labels promoted to inline headings | Text boxes, layout tables, or a two-column grid |
| Standard fonts (Calibri / Arial / Garamond) | Machina or any face the parser may not have |
| Real Word heading styles | Bold body text pretending to be a heading |
| Plain bullet characters (`•`) | Icon fonts, emoji, star or dot skill ratings |
| Dates as `Feb 2025 – Aug 2025`, inline with the entry | Dates only in a graphic timeline or a side rail |
| Contact details in the body, top of page | Contact details in a header/footer — many parsers drop those entirely |
| Skills as comma-separated text | Skills as a table or chart |

Assert both outputs carry byte-identical bullet text. A divergence between the PDF and the DOCX means one of them was built from something other than the profile IDs, which is a bug.

### 8.9 Why this design is strictly better than free-form tailoring

- **Fit is proven arithmetically before a file is written**, instead of discovered after rendering.
- **The visual identity is stable across every application.** Recruiters at the same company seeing two applications see the same document, which reads as deliberate rather than generated.
- **The blast radius of a model error is three slots.** It cannot reorder sections, drop education, invent a summary, or restructure experience, because the schema gives it no field to do so.
- **Experience is effectively immutable**, which is correct: it is the part most likely to be cross-checked against LinkedIn, and the part where inconsistency between applications is most damaging.
- **Diffing two generated CVs is trivial** — the diff is always confined to the projects block and three skill rows.

---

## 9. Stage 4b — Narrative Planner and Stage 5b — Letter Writer

### 9.1 Planner (Groq, strict JSON, `temperature=0.2`)

Input: job analysis + match result + the 4–6 highest-scoring evidence blocks (never the whole profile) + truth layer.

Output:

```json
{
  "main_theme": "applied ML for decisions under uncertainty",
  "secondary_theme": "quantitative rigour from an MSc plus hands-on projects",
  "evidence_order": ["ev.ophis_ml", "ev.race_strategy", "ev.database"],
  "company_angle": "their predictive-maintenance work is the same class of problem as my thesis",
  "opening_strategy": "problem_first",
  "avoid": ["AWS", "production deployment", "team leadership"],
  "language": "en",
  "target_words": 300
}
```

Separating planning from writing is what makes a 20B model competitive here. Asked to plan *and* write at once, it defaults to a generic template. Given a plan, it only has to execute.

### 9.2 Writer (Groq, `temperature=0.5–0.7`)

The prompt contains **only**:
- job title, company, one-paragraph company context (from the posting or a human-maintained `companies.yaml`)
- the narrative plan
- the `facts` arrays of *only* the selected evidence blocks
- the style guide from `profile/style.yaml` (voice samples + banned phrases)
- the `avoid` list, restated as a hard rule

The prompt does **not** contain: the full profile, the full job description, or the CV. Less context ⇒ less to hallucinate from, and a far cheaper call.

**Structure to request (4 paragraphs, ≤ 320 words, one page):**

1. **Opening** — why *this* role at *this* company, specific and non-generic. One sentence of genuine reason, no "I am writing to apply for".
2. **Evidence paragraph 1** — the strongest block, told as a problem → approach → outcome.
3. **Evidence paragraph 2** — the second block, connected to a responsibility named in the posting.
4. **Close** — what they get, availability/logistics if relevant, a plain sign-off.

**`style.yaml` banned-phrase list** (enforced by regex post-check, not just by prompt):

```
"I am writing to apply", "I was excited to see", "passionate about",
"perfect fit", "proven track record", "leverage my skills",
"I believe my background", "delve", "tapestry", "in today's fast-paced",
"dynamic environment", "align with your needs", "wealth of experience",
"as a highly motivated"
```

Hit ⇒ automatic regeneration with the offending phrase quoted back. Max 2 retries, then surface the draft to the human with the phrases highlighted rather than looping forever.

### 9.3 Language

If `job_analysis.language != "en"` and the profile declares that language at `confidence: high`, write in that language; otherwise write in English and log a note. Never machine-translate a letter into a language the candidate cannot sustain in an interview.

---

## 10. Stage 6 — Fact Checker

This is the safety net and it runs on **local Qwen2.5 plus deterministic gates**. The deterministic part catches most of it; the model catches the semantic drift.

### 10.1 Deterministic gates (run first, they are free)

```python
def deterministic_gates(text, truth):
    fails = []
    # 1. Numbers
    for n in re.findall(r"\d[\d,._]*", text):
        if normalize_num(n) not in truth.allowed_numbers:
            fails.append(("UNSUPPORTED_NUMBER", n))
    # 2. Forbidden entities (word-boundary, case-insensitive)
    for term in truth.forbidden_claims:
        if re.search(rf"\b{re.escape(term)}\b", text, re.I):
            fails.append(("FORBIDDEN_CLAIM", term))
    # 3. Gated verbs
    for verb in truth.gated_verbs:
        if re.search(rf"\b{verb}\b", text, re.I) and not verb_backed(verb, truth):
            fails.append(("UNSUPPORTED_SENIORITY_VERB", verb))
    # 4. Named entities not in allowed_orgs (spaCy or a curated org/tool list)
    for ent in extract_orgs_and_products(text):
        if canon(ent) not in truth.allowed_orgs | truth.allowed_skills:
            fails.append(("UNKNOWN_ENTITY", ent))
    return fails
```

### 10.2 Model-based claim audit

Split the letter into sentences. For each sentence, ask the local model, **one sentence per call** (short prompts are where small models are reliable):

```
ALLOWED FACTS:
1. developed machine learning systems for quantitative research
2. built probabilistic and Monte Carlo risk models
...

SENTENCE:
"At Ophis Research I developed machine-learning systems for quantitative research,
including Monte Carlo models of downside risk."

Is every factual assertion in SENTENCE supported by the ALLOWED FACTS?
Answer JSON only: {"verdict": "SUPPORTED" | "UNSUPPORTED" | "OVERSTATED",
                   "unsupported_span": "<quote or null>"}
```

`OVERSTATED` is a distinct verdict on purpose — "I have deep expertise in X" from "used X in one project" is the commonest and most damaging failure, and it is not caught by entity matching.

### 10.3 Failure policy

| Result | Action |
|--------|--------|
| All SUPPORTED | Proceed to packaging |
| Any UNSUPPORTED / OVERSTATED | Regenerate the letter with the offending sentence quoted and an explicit correction instruction. Max **2** retries. |
| Still failing after 2 retries | **Hard stop.** Write the draft to `run/` marked `REJECTED`, print the failing claims, exit non-zero. Never write a rejected letter to `output/`. |

A human staring at a rejected draft with three highlighted sentences is a *good* outcome. Silently shipping a plausible lie is the only truly bad outcome this system can produce.

### 10.4 The CV needs checking too

Cheaper, because the CV is assembled from IDs: assert that every printed string is byte-identical to either `bullet.text`, an approved `variant.text`, a `metric.claim`, or a profile field. Any string not traceable to an ID ⇒ abort. This is a full guarantee, not a probabilistic one, and it is why the CV path uses no free-form generation.

---

## 11. Stage 7 — Packaging, naming, provenance

```
output/<CompanySlug>_<RoleSlug>/
    Simon_Sazonov_<Company>_<Role>_CV.pdf
    Simon_Sazonov_<Company>_<Role>_CV.docx
    Simon_Sazonov_<Company>_<Role>_Cover_Letter.pdf
    Simon_Sazonov_<Company>_<Role>_Cover_Letter.docx
    provenance.json
```

Slugs: ASCII, `[A-Za-z0-9_]`, title-cased, truncated to 40 chars. Collisions get `_v2`, `_v3` — **never overwrite** a previously generated application.

`provenance.json`:

```json
{
  "generated_at": "2026-09-23T14:02:11Z",
  "profile_git_sha": "a1b2c3d",
  "job": {"company": "BMW", "title": "Graduate Data Scientist", "url": "...", "csv_row": 412},
  "used_ids": ["ophis.b1", "ophis.b2", "proj.wikiart.b1", "m.airbnb.query_time"],
  "cv_config": { },
  "narrative_plan": { },
  "coverage": {"required": 0.83, "preferred": 0.40},
  "fact_check": {"sentences": 11, "supported": 11, "retries": 1},
  "llm_calls": [{"stage": "01", "model": "openai/gpt-oss-20b", "provider": "groq",
                 "prompt_tokens": 1840, "completion_tokens": 310, "ms": 640}],
  "page_count": {"cv": 1, "letter": 1}
}
```

Provenance is what makes this auditable six months later when an interviewer asks about a line on the CV.

**PDF conversion:** LibreOffice headless (`soffice --headless --convert-to pdf`) is the reliable cross-platform route and preserves the DOCX layout. Docx2pdf on macOS needs Word installed. Do not render the PDF independently from the DOCX — two rendering paths means two layouts.

---

## 12. Model routing

Extend the existing chain with **per-task routing** rather than one global chain.

```python
TASK_ROUTING = {
  "job_analysis":     ["groq:openai/gpt-oss-20b", "ollama:pitwall-qwen2.5"],
  "synonym_resolve":  ["ollama:pitwall-qwen2.5", "groq:openai/gpt-oss-20b"],
  "slot_select":      ["groq:openai/gpt-oss-20b", "deterministic_fallback"],   # 3 slots only
  "narrative_plan":   ["groq:openai/gpt-oss-20b"],
  "letter_write":     ["groq:openai/gpt-oss-20b"],
  "fact_check":       ["ollama:pitwall-qwen2.5", "groq:openai/gpt-oss-20b"],
  "classification":   ["ollama:pitwall-qwen2.5"],
}
```

| Task | Model | Why |
|------|-------|-----|
| Job analysis | Groq gpt-oss-20b | strict schema JSON, needs real comprehension, one call per job |
| Synonym resolution | local Qwen | trivial, cacheable forever, would waste quota |
| Slot selection | Groq | genuine judgement, but output is a tiny JSON and the solver overrides any infeasible choice |
| Narrative planning | Groq | the step that most improves letter quality |
| Letter writing | Groq | best available prose |
| Fact checking | local Qwen | many short calls; a second, *independent* model is the point — do not fact-check with the model that wrote the text |

**Important:** fact-checking with a different model family from the writer is deliberate. A model grading its own output is systematically biased toward passing it.

### 12.1 Quota discipline

- Set `reasoning_effort=low` for everything except `letter_write`, where `medium` is worth testing.
- `temperature=0` for all structured-output stages; sampling is pure downside when the output is a schema.
- Cache aggressively on `sha256(prompt + model + params)`. Re-running a job after a profile edit should only re-run the stages downstream of the edit.
- Budget target: ~4–6k tokens per complete application. Log actual usage per run and alert if a run exceeds 15k — that means a retry loop is thrashing.

### 12.2 Trace everything

`run/<id>/trace.jsonl`, one line per call: stage, model, provider, prompt hash, token counts, latency, retry index, outcome. Without this you cannot debug quality regressions, and quality regressions are the failure mode you will actually face.

---

## 13. CLI

```bash
# 1. sanity-check the profile (run this in CI on every profile commit)
app validate-profile

# 2. list jobs from the CSV with a pre-computed match score
app jobs --csv jobs/2026-09.csv --min-score 0.5

# 3. analyze one job, show matched / partial / missing — no generation, no cost beyond stage 1
app analyze --job 412

# 4. full pipeline
app generate --job 412 --cv --letter

# 5. re-run only the letter with a nudge, reusing cached stages 1–3
app generate --job 412 --letter --plan-hint "emphasise the thesis, not the finance work"

# 6. explain a decision
app explain --job 412        # prints rationale + scores per bullet

# 7. batch
app batch --csv jobs/2026-09.csv --top 10
```

`--dry-run` prints the CV config and narrative plan without calling the writer or touching `output/`. Use it constantly during development.

---

## 14. Testing

### 14.1 Unit

- `test_matcher.py` — golden job fixtures → expected matched/missing sets.
- `test_truth_layer.py` — a missing requirement must appear in `forbidden_claims`.
- `test_renderer_budget.py` — property test: for **any** valid config, rendered PDF is exactly 1 page.
- `test_layout_frozen.py` — render 20 different job configs; assert section order, experience entries, experience bullet counts and education block are **byte-identical** across all 20. Only the projects block and skill rows may differ.
- `test_line_cost.py` — every declared variant has the same `line_cost` as its canonical bullet; every project's `line_cost` matches the sum of its parts.
- `test_project_solver.py` — for every job fixture, the chosen trio satisfies `Σ line_cost ≤ 11` and is the true optimum (verified against exhaustive enumeration).
- `test_skill_packer.py` — every packed row is ≤ `max_chars`, contains no `printable: false` skill, and always includes pinned items.
- `test_taxonomy.py` — every tag in `profile/` is canonical; no orphan aliases.
- `test_ids.py` — all IDs unique, all `evidence:` references resolve.

### 14.2 Adversarial fact-checker suite (the most valuable tests you will write)

Hand-write ~40 sentences, half supported, half subtly false, and assert the checker's verdicts:

```
"I have production experience deploying models on AWS."         → UNSUPPORTED
"I reduced query time from 15,832 ms to 63 ms."                 → SUPPORTED
"I reduced query time by over 99%."                             → UNSUPPORTED (derived number)
"I led a team of data scientists."                              → UNSUPPORTED (gated verb)
"I built CNN models for artwork classification."                → SUPPORTED
"I have deep expertise in computer vision."                     → OVERSTATED
"I used PyTorch for image classification."                      → SUPPORTED
"I have three years of professional ML experience."             → UNSUPPORTED
```

Run this suite on every prompt change. A prompt edit that drops recall here is a regression, no matter how much nicer the prose got.

### 14.3 End-to-end evaluation — the 30-job benchmark

Run the pipeline over 30 real, varied postings. For each, the human scores 1–5:

| Dimension | What you are really asking |
|-----------|---------------------------|
| Factual accuracy | Did it claim anything untrue? (any score < 5 is a bug, not a preference) |
| Relevance | Did it pick the right evidence? |
| Naturalness | Would a human have written this? |
| Company specificity | Could this letter be sent to a different company unchanged? (if yes → 1) |
| CV tailoring | Is the emphasis right for this role? |
| Edit burden | % of text changed before sending |

Track the mean per dimension across prompt versions in a simple CSV. **Ship when edit burden is consistently ≤ 30%.** Improve the profile data and the prompts before you consider a bigger model — with this architecture, data quality dominates model quality.

---

## 15. Failure modes to design against

| Failure | Mitigation |
|---------|-----------|
| Model invents a job requirement | `evidence_quote` substring check (§5) |
| Model adds a missing skill to close a gap | missing requirements auto-added to `forbidden_claims` (§7.1) |
| Model derives a new number ("99% faster") | numeric allowlist, exact match only (§10.1) |
| Model inflates seniority | gated verbs + OVERSTATED verdict (§7.4, §10.2) |
| CV silently spills to 2 pages | line-cost arithmetic proves the fit *before* writing (§8.4); page-count assertion is a backstop (§8.8) |
| Model reorders sections or drops education | structurally impossible — no such field exists in the output schema (§8.7) |
| A rephrased bullet wraps to an extra line and breaks the frame | line-neutrality check at profile-validation time (§7.3, §8.4) |
| The model picks 3 projects that cannot fit | solver enumerates all feasible trios and overrides, logging the override (§8.5) |
| Two applications to the same company look structurally different | frozen frame guarantees they cannot (§8.9) |
| Groq rate-limited mid-run | per-task chain with local fallback; never degrade the *writer* silently — log it in provenance |
| Letter reads like every other AI letter | planner/writer split + banned-phrase regex + human voice samples (§9) |
| Profile edit silently breaks old runs | IDs immutable; `profile_git_sha` in provenance |
| Two jobs produce identical letters | assert pairwise similarity across recent outputs < 0.8; warn if exceeded |
| Same company applied twice | check `output/` for an existing folder with the same company slug and warn |

---

## 16. What NOT to build

- ❌ A web app in v1. A CLI over a well-tested library is faster to build and faster to trust. Add a thin Streamlit page in v1.1 *over the same functions*.
- ❌ "Send the whole CV and the whole job description, ask it to tailor." This is the thing being replaced.
- ❌ A fixed library of complete paragraphs stitched together. That is exactly what produces the recognisable AI-letter feel. Blocks are **evidence**, not prose.
- ❌ Embeddings/RAG in v1. The profile is ~60 records. It fits in a prompt. Vector search here is complexity with no payoff.
- ❌ Auto-submission. Human approval before anything is sent, always.
- ❌ Letting the model edit `profile/`. The profile is human-authored, full stop. A model *may* propose a diff to a review queue; it may never write.
- ❌ Fine-tuning. There is nothing to fine-tune on, and the failure mode being solved is factual, not stylistic.

---

## 17. Build order

| Milestone | Deliverable | Why first |
|-----------|-------------|-----------|
| **M0** | `profile/` schemas + `validate-profile` + taxonomy | Nothing works without trustworthy data; this also tells Simon exactly what to write |
| **M1** | Stage 1 job analyzer + cache + fixtures | Cheap, testable, immediately useful on its own |
| **M2** | Stage 2 matcher + Stage 3 truth layer + `app analyze` | Delivers value with **zero generation** — a gap report per job |
| **M2.5** | Rebuild the Canva design as an HTML/CSS template + WeasyPrint, plus the single-column DOCX variant; verify both against the reference PDF | Canva cannot be driven programmatically (§8.1a); everything downstream needs a real template |
| **M3** | `layout.yaml` + frozen template + `calibrate` + line-cost model + deterministic slot filling (no LLM) | Proves the frame and the fit guarantee before any AI touches it. This milestone alone already produces a correct CV. |
| **M4** | LLM slot selector on top of M3, solver-gated | Now the CV is tailored, and cannot break the frame |
| **M5** | Planner + writer + banned-phrase check | First cover letters |
| **M6** | Fact checker + adversarial test suite | Makes M5 safe to actually use |
| **M7** | Packager, provenance, batch mode | Throughput |
| **M8** | 30-job evaluation, prompt iteration | The part that determines whether this is good |

**M2 alone already justifies the project** — knowing precisely which requirements are matched, partial, and missing for every job in the CSV is valuable before a single document is generated. Ship it early and use it.

---

## 18. Suggested dependencies

```
pydantic>=2          # models + validation
PyYAML               # profile loading
jsonschema           # schema enforcement on LLM output
python-docx          # document building
docxtpl              # optional, if templating rather than building
weasyprint           # HTML/CSS -> PDF for the designed primary output (§8.1a)
pypdf                # page-count assertion
httpx                # Groq / Ollama calls
tenacity             # retry with backoff, distinguishing 429-rate-limit from quota
rapidfuzz            # alias fuzzy matching, similarity checks
spacy (sm model)     # NER for the entity gate — optional, a curated list also works
rich                 # CLI output
pytest               # tests
```

Keep it boring. Every dependency added is something that can break between Simon and a submitted application.

---

## 19. The one-sentence summary to keep on a sticky note

> **The model selects and phrases. The program verifies and prints. If a sentence cannot be traced to an ID in `profile/`, it does not ship.**
