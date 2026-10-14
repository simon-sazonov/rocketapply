"""SQLite connection, schema and repositories — §4.2 of PITWALL_PLAN.md.

Raw `sqlite3` with a thin repository layer; no ORM. The DDL below is the
plan's schema verbatim, plus a `quota` table for the free-tier guards of §13.2
and a `run_progress` table so the Streamlit worker thread can stream progress
without touching the UI thread (§10, "poll a progress row in SQLite").
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .config import Settings, get_settings

SCHEMA_VERSION = 10

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─────────────────────────── inputs ───────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    aliases           TEXT,            -- JSON array
    negative_keywords TEXT,            -- JSON array
    linkedin_slug     TEXT,
    domain            TEXT,
    series            TEXT,
    careers_url       TEXT,            -- §2.2
    ats_kind          TEXT,            -- greenhouse|lever|ashby|… discovered once
    ats_token         TEXT,            -- the board slug on that ATS
    country           TEXT,
    tier              INTEGER DEFAULT 2,
    active            INTEGER DEFAULT 1,
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS roles (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL UNIQUE,
    aliases    TEXT,                   -- JSON array
    family     TEXT NOT NULL,
    seniority  TEXT,                   -- JSON array
    keywords   TEXT,                   -- JSON array
    priority   INTEGER DEFAULT 2,
    active     INTEGER DEFAULT 1,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ─────────────────────────── queries ───────────────────────────
CREATE TABLE IF NOT EXISTS queries (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    role_id       INTEGER REFERENCES roles(id)     ON DELETE CASCADE,
    template_id   TEXT NOT NULL,
    query_text    TEXT NOT NULL,
    query_hash    TEXT NOT NULL UNIQUE,   -- sha1(provider|query_text)
    priority      REAL DEFAULT 0,
    status        TEXT DEFAULT 'pending', -- pending|running|done|failed|skipped
    provider      TEXT,
    results_count INTEGER DEFAULT 0,
    error         TEXT,
    last_run_at   TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_queries_status ON queries(status, priority DESC);

-- ─────────────────────────── raw results ───────────────────────
CREATE TABLE IF NOT EXISTS search_results (
    id            INTEGER PRIMARY KEY,
    query_id      INTEGER REFERENCES queries(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    rank          INTEGER,
    url           TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title         TEXT,
    snippet       TEXT,
    raw_json      TEXT,
    processed     INTEGER DEFAULT 0,
    fetched_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(query_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS ix_results_canon ON search_results(canonical_url);
CREATE INDEX IF NOT EXISTS ix_results_processed ON search_results(processed);

-- ─────────────────────────── people ────────────────────────────
CREATE TABLE IF NOT EXISTS candidates (
    id                INTEGER PRIMARY KEY,
    linkedin_slug     TEXT NOT NULL UNIQUE,   -- canonical dedup key
    profile_url       TEXT NOT NULL,
    full_name         TEXT,
    headline          TEXT,
    current_title     TEXT,
    current_company   TEXT,
    location          TEXT,
    evidence_count    INTEGER DEFAULT 0,      -- distinct queries that surfaced them
    template_count    INTEGER DEFAULT 0,      -- distinct query TEMPLATES (§1.4)
    first_seen_at     TEXT DEFAULT (datetime('now')),
    last_seen_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_candidates_name ON candidates(full_name);

-- many-to-many: which SERP rows are evidence for which person
CREATE TABLE IF NOT EXISTS candidate_sources (
    candidate_id     INTEGER REFERENCES candidates(id)     ON DELETE CASCADE,
    search_result_id INTEGER REFERENCES search_results(id) ON DELETE CASCADE,
    PRIMARY KEY (candidate_id, search_result_id)
);

-- ─────────────────────────── scoring ───────────────────────────
CREATE TABLE IF NOT EXISTS scores (
    id                  INTEGER PRIMARY KEY,
    candidate_id        INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    company_id          INTEGER REFERENCES companies(id),
    role_id             INTEGER REFERENCES roles(id),
    heuristic_score     REAL,
    llm_score           REAL,
    final_score         REAL,
    verdict             TEXT,      -- strong|possible|weak|reject|needs_review|pending_llm
    is_current_employee INTEGER,
    company_match       TEXT,      -- exact|parent|subsidiary|same_name_different_org|none
    role_match          TEXT,      -- exact|adjacent|unrelated
    seniority           TEXT,
    confidence          REAL,
    reasons             TEXT,      -- JSON array
    red_flags           TEXT,      -- JSON array
    model               TEXT,
    provider            TEXT,      -- which LLM answered, so mixed-provider runs audit
    prompt_version      TEXT,
    scored_at           TEXT DEFAULT (datetime('now')),
    UNIQUE(candidate_id, company_id, role_id, prompt_version)
);
CREATE INDEX IF NOT EXISTS ix_scores_final ON scores(final_score DESC);
CREATE INDEX IF NOT EXISTS ix_scores_candidate ON scores(candidate_id);

-- ─────────────────────────── CRM ───────────────────────────────
CREATE TABLE IF NOT EXISTS crm (
    candidate_id  INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    status        TEXT DEFAULT 'new',  -- new|saved|ignored|contacted|replied|meeting|closed
    note          TEXT,
    tags          TEXT,                -- JSON array
    contacted_at  TEXT,
    followup_at   TEXT,
    updated_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_crm_status ON crm(status, followup_at);

CREATE TABLE IF NOT EXISTS blocklist (
    linkedin_slug TEXT PRIMARY KEY,
    reason        TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

-- ─────────────────────────── bookkeeping ───────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY,
    command          TEXT,          -- run|search|extract|score|ui — which entry point
    started_at       TEXT DEFAULT (datetime('now')),
    finished_at      TEXT,
    params           TEXT,          -- JSON
    queries_planned  INTEGER DEFAULT 0,
    queries_executed INTEGER DEFAULT 0,
    results_fetched  INTEGER DEFAULT 0,
    candidates_new   INTEGER DEFAULT 0,
    llm_calls        INTEGER DEFAULT 0,
    llm_tokens_in    INTEGER DEFAULT 0,
    llm_tokens_out   INTEGER DEFAULT 0,
    cost_estimate    REAL DEFAULT 0,
    status           TEXT DEFAULT 'running',
    error            TEXT
);

-- free-tier guards (§13.2): one row per provider per UTC day
CREATE TABLE IF NOT EXISTS quota (
    provider  TEXT NOT NULL,
    day       TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    requests  INTEGER DEFAULT 0,
    credits   INTEGER,                -- remaining, for credit-based providers
    -- When a monthly allowance refills, as the provider itself reports it.
    -- Hunter's 50 credits reset on a date its /v2/account endpoint returns;
    -- guessing "the 1st" locally would show the owner a date that is wrong.
    reset_at  TEXT,
    PRIMARY KEY (provider, day)
);

-- live progress for the Streamlit worker thread (§10)
CREATE TABLE IF NOT EXISTS run_progress (
    run_id     INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    stage      TEXT,
    message    TEXT,
    current    INTEGER DEFAULT 0,
    total      INTEGER DEFAULT 0,
    cancelled  INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ─────────────────────────── job openings (§2.2) ───────────────────────
-- Answers "who do I write to, and why now?" by cross-referencing live postings
-- against people already in `candidates`.
CREATE TABLE IF NOT EXISTS job_sources (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,          -- ats_json|aggregator|career_page
    name           TEXT NOT NULL UNIQUE,
    base_url       TEXT,
    company_id     INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    enabled        INTEGER DEFAULT 1,
    last_polled_at TEXT,
    last_status    TEXT,                   -- 'ok', 'http 403', 'robots: disallowed'…
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    -- Nullable on purpose: a posting may name a company that is not in
    -- companies.csv, and losing it would be worse than not linking it.
    company_id       INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    source_id        INTEGER REFERENCES job_sources(id) ON DELETE CASCADE,
    external_id      TEXT,
    title            TEXT NOT NULL,
    description      TEXT,
    location         TEXT,
    employment_type  TEXT,
    url              TEXT NOT NULL,
    posted_at        TEXT,
    first_seen_at    TEXT DEFAULT (datetime('now')),
    last_seen_at     TEXT DEFAULT (datetime('now')),
    -- A posting that stops appearing is CLOSED, never deleted: "you missed this
    -- one" is history worth keeping (§2.2, and Appendix C forbids the deletion).
    status           TEXT DEFAULT 'open',   -- open|closed|unknown
    raw_json         TEXT,
    relevance_score  REAL,
    relevance_reasons TEXT,                 -- JSON array
    relevance_model  TEXT,
    relevance_prompt_version TEXT,
    UNIQUE(source_id, url)
);
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id, status);
CREATE INDEX IF NOT EXISTS ix_jobs_status  ON jobs(status, posted_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_seen    ON jobs(last_seen_at);

-- One row per stage a run passed through, so a finished run can still be
-- explained: what it did, in what order, how long each part took. `run_progress`
-- holds only the live cursor and is overwritten in place; this is the history.
CREATE TABLE IF NOT EXISTS run_events (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    stage      TEXT NOT NULL,
    message    TEXT,
    current    INTEGER DEFAULT 0,
    total      INTEGER DEFAULT 0,
    started_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_run_events_run ON run_events(run_id, id);

-- ────────────── contact enrichment (§6 of PITWALL_V3_EMAIL_ENRICHMENT.md) ──
-- Four tables, and the split between them is the whole design: `contact_emails`
-- is about a *person*, `company_domains` and `domain_address_samples` are about
-- a *company*, and the company-side corpus is what makes the person-side answer
-- free. One paid Hunter call feeds `domain_address_samples` and every later
-- candidate at that company is then resolved locally, for nothing.
CREATE TABLE IF NOT EXISTS company_domains (
    id                  INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    domain              TEXT NOT NULL,
    -- careers_url|serp|search|manual|hunter — §2.1's resolution order, recorded
    -- so the UI can say where a wrong domain came from.
    source              TEXT,
    pattern             TEXT,               -- first.last | flast | first | …
    pattern_sample_size INTEGER DEFAULT 0,  -- §2.3: THE confidence signal
    pattern_agreement   REAL,               -- share of samples the pattern explains
    last_harvested_at   TEXT,
    -- The owner's correction. Set, and resolution stops looking: a wrong domain
    -- poisons every address derived from it, so the override outranks every
    -- automatic source for good.
    manual_override     INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE (company_id, domain)
);
CREATE INDEX IF NOT EXISTS ix_company_domains_company ON company_domains(company_id);

-- The corpus that makes patterns free. Two known addresses at a domain reveal
-- the format; you never need the target person's own address to help them.
CREATE TABLE IF NOT EXISTS domain_address_samples (
    id                INTEGER PRIMARY KEY,
    company_domain_id INTEGER NOT NULL REFERENCES company_domains(id) ON DELETE CASCADE,
    email             TEXT NOT NULL,
    person_name       TEXT,
    source_url        TEXT,
    source            TEXT,                 -- harvest|hunter_domain|manual
    found_at          TEXT DEFAULT (datetime('now')),
    UNIQUE (company_domain_id, email)
);
CREATE INDEX IF NOT EXISTS ix_domain_samples_domain ON domain_address_samples(company_domain_id);

CREATE TABLE IF NOT EXISTS contact_emails (
    id              INTEGER PRIMARY KEY,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    -- verified|probable|guess|not_found — §4. Earned by evidence, never by a
    -- number, and the UI is required to render the evidence alongside it.
    status          TEXT NOT NULL DEFAULT 'guess',
    -- corroborated|pattern|github|hunter_finder|hunter_domain|manual
    source          TEXT NOT NULL,
    evidence_json   TEXT,        -- pattern, sample size, corroborating URL, MX host
    -- The PROVIDER'S own score, and nothing else. NULL for anything derived
    -- locally: inventing a percentage for a pattern guess is exactly the
    -- "number invites trust" failure §4 forbids.
    confidence      REAL,
    mx_valid        INTEGER,
    is_role_address INTEGER DEFAULT 0,
    is_disposable   INTEGER DEFAULT 0,
    retrieved_at    TEXT DEFAULT (datetime('now')),
    raw_json        TEXT,        -- provider payload, API key redacted
    UNIQUE (candidate_id, email)
);
CREATE INDEX IF NOT EXISTS ix_contact_emails_candidate ON contact_emails(candidate_id, status);

-- The audit trail. `quota` is the budget; this is the history of how it went.
--
-- `candidate_id` is ON DELETE SET NULL, not CASCADE, and that is deliberate:
-- §6.1 makes blocklisting an erasure, and an erased person must leave no
-- contact data behind — but "3 credits were spent on 12 August" is budget
-- history, not personal data, and losing it would make the ledger lie. Nothing
-- written here may contain an address; `enrich.redact` is what enforces that.
CREATE TABLE IF NOT EXISTS enrichment_log (
    id            INTEGER PRIMARY KEY,
    candidate_id  INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    company_id    INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    step          TEXT NOT NULL,
    provider      TEXT,
    -- REAL, not INTEGER: Hunter's Email Verifier costs 0.5 credits, verified
    -- against help.hunter.io on 1 Sep 2026. An integer column would round the
    -- ledger away from the truth on every verification.
    credits_spent REAL DEFAULT 0,
    outcome       TEXT,
    detail        TEXT,          -- never an email address — see above
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_enrichment_log_candidate ON enrichment_log(candidate_id, id);
CREATE INDEX IF NOT EXISTS ix_enrichment_log_created ON enrichment_log(created_at);

-- ─────────────────────────── job search (v6) ───────────────────────────
-- The personal job search, built to job_search_assistant_engineering_spec.md.
-- It shares `jobs` and `job_sources` with the Openings page — one posting is
-- one row whoever polled it — and adds only what is about the SEARCH: the
-- owner's target employers, how each posting scored against their profile,
-- what they decided about it, and the applications that followed.

-- The job-search employer list, from config/target_companies.csv. Separate
-- from `companies` on purpose: that table is what the people sweep searches,
-- and adding a hundred job targets must not add a hundred LinkedIn sweeps.
CREATE TABLE IF NOT EXISTS target_companies (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,
    aliases               TEXT,            -- JSON array
    country               TEXT,            -- ISO 3166 alpha-2 where known
    cities                TEXT,            -- JSON array
    industry              TEXT,
    priority              TEXT DEFAULT 'B',   -- A|B|C — a search priority, not a rating
    careers_url           TEXT,
    ats_kind              TEXT,
    ats_token             TEXT,
    sponsorship           TEXT DEFAULT 'unknown',  -- yes|sometimes|no|unknown, the owner's note
    visa_notes            TEXT,
    tags                  TEXT,            -- JSON array
    active                INTEGER DEFAULT 1,
    -- The same organisation on the people-search list, when it is there, so a
    -- posting can say "you know 3 people at this company".
    networking_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    updated_at            TEXT DEFAULT (datetime('now'))
);

-- One row per posting: did it pass the hard filters, and how well does it fit?
-- Recomputed on every run — deterministic and cheap — so a change to the
-- profile re-ranks everything. `config_hash` says which profile produced it.
CREATE TABLE IF NOT EXISTS job_matches (
    job_id        INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    config_hash   TEXT,
    passed        INTEGER NOT NULL DEFAULT 0,
    filter_reasons TEXT,                  -- JSON [{code, message}]
    score         REAL,
    label         TEXT,                   -- strong|good|fair|weak
    components    TEXT,                   -- JSON {name: {value, label, weight}}
    reasons_plus  TEXT,                   -- JSON array: why it matched
    reasons_minus TEXT,                   -- JSON array: concerns, "?"/"-" prefixed
    matched_skills TEXT,                  -- JSON array
    missing_skills TEXT,                  -- JSON array
    visa_fit      TEXT,                   -- ok|explicit|likely|unknown|no
    ai_score      REAL,
    ai_reasons    TEXT,                   -- JSON array
    ai_model      TEXT,
    scored_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_job_matches_passed ON job_matches(passed, score DESC);

-- What the owner decided about a vacancy. Keyed by cluster — the vacancy, not
-- one source's copy of it — so dismissing a job on Arbeitnow also dismisses
-- the same job arriving from the company's ATS tomorrow.
CREATE TABLE IF NOT EXISTS job_review (
    cluster_id       INTEGER PRIMARY KEY,
    seen_at          TEXT,
    dismissed_at     TEXT,
    dismissed_reason TEXT,
    note             TEXT,
    feedback         TEXT,                 -- good_match|bad_match (§91)
    updated_at       TEXT DEFAULT (datetime('now'))
);

-- The application tracker (§37-38). `cluster_id` is NULL for an application
-- entered by hand for a job the search never saw.
CREATE TABLE IF NOT EXISTS applications (
    id                   INTEGER PRIMARY KEY,
    cluster_id           INTEGER,
    job_id               INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    target_company_id    INTEGER REFERENCES target_companies(id) ON DELETE SET NULL,
    company_name         TEXT NOT NULL,
    company_norm         TEXT,
    title                TEXT NOT NULL,
    location             TEXT,
    status               TEXT NOT NULL DEFAULT 'SHORTLISTED',
    date_applied         TEXT,
    cv_version           TEXT,
    cover_letter_version TEXT,
    application_url      TEXT,
    contact_person       TEXT,
    notes                TEXT,
    salary               TEXT,
    response_date        TEXT,
    next_action          TEXT,
    next_action_date     TEXT,
    created_at           TEXT DEFAULT (datetime('now')),
    updated_at           TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_applications_cluster ON applications(cluster_id);
CREATE INDEX IF NOT EXISTS ix_applications_company ON applications(company_norm);

-- Every status change, so "when did this go to INTERVIEW?" has an answer and
-- the later response-rate analytics (§80) have something to count.
CREATE TABLE IF NOT EXISTS application_events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    note           TEXT,
    at             TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_application_events_app ON application_events(application_id, id);

-- v7: one row per search a keyword source runs (query x country). Two jobs:
-- the cursor, so the next visit asks only for what is newer than the last one
-- (a rotation that skips a day then loses nothing), and the yield, so the
-- searches that never find a relevant job can make way for ones that do.
CREATE TABLE IF NOT EXISTS source_queries (
    source          TEXT NOT NULL,
    query           TEXT NOT NULL,
    country         TEXT NOT NULL DEFAULT '',
    first_run_at    TEXT DEFAULT (datetime('now')),
    last_fetched_at TEXT,             -- the last COMPLETE read; NULL = never
    backfilled      INTEGER DEFAULT 0,
    runs            INTEGER DEFAULT 0,
    requests        INTEGER DEFAULT 0,
    found           INTEGER DEFAULT 0,
    last_total      INTEGER,          -- what the source said the search matched
    PRIMARY KEY (source, query, country)
);

-- v7: ATS boards found from the links inside postings (jobsearch/ats_harvest.py).
-- `verified` = the board's postings name the employer that linked to it, or it
-- sits at that employer's whole name. A failed check keeps its reason and is
-- not retried for 30 days.
CREATE TABLE IF NOT EXISTS discovered_boards (
    kind          TEXT NOT NULL,
    token         TEXT NOT NULL,
    company       TEXT,
    company_norm  TEXT,
    verified      INTEGER DEFAULT 0,
    reason        TEXT,
    postings      INTEGER DEFAULT 0,
    active        INTEGER DEFAULT 1,
    first_seen_at TEXT DEFAULT (datetime('now')),
    checked_at    TEXT,
    PRIMARY KEY (kind, token)
);

-- Small named markers a stage keeps between runs (the harvest's last job id).
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- v10: RocketApply. A CSV export is a named batch of applications, so the ones
-- that went out together can be marked applied together. `generation_dir` is
-- the folder the CSV pointed at, frozen at export: `apply tidy` leaves those
-- folders where they are, so a path in a CSV never goes stale.
CREATE TABLE IF NOT EXISTS export_batches (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    csv_path    TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    applied_at  TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS export_batch_items (
    batch_id        INTEGER NOT NULL REFERENCES export_batches(id) ON DELETE CASCADE,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    generation_dir  TEXT,
    PRIMARY KEY (batch_id, application_id)
);
CREATE INDEX IF NOT EXISTS ix_export_items_app ON export_batch_items(application_id);

-- v10: a drafted LinkedIn note to one person about one application. Drafts
-- only — nothing in the codebase sends them. `person_key` is the profile URL
-- when there is one, else the lower-cased name.
CREATE TABLE IF NOT EXISTS outreach_notes (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    person_key      TEXT NOT NULL,
    person_name     TEXT,
    person_headline TEXT,
    profile_url     TEXT,
    kind            TEXT NOT NULL DEFAULT 'connection_note',
    text            TEXT NOT NULL,
    edited          INTEGER DEFAULT 0,
    checks          TEXT,                 -- JSON array of gates it failed, [] when clean
    model           TEXT,
    prompt_version  TEXT,
    tokens          INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE (application_id, person_key, kind)
);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
"""

#: v9 -> v10: what RocketApply generated for an application and whether it was
#: reviewed. `generation_dir` is the application folder under output/,
#: `generation_mode` is cv | cv_letter | cv_letter_audit, `letter_state` is the
#: pipeline's clean | review | unsent | "" (no letter).
APPLICATION_GENERATION_COLUMNS: tuple[str, ...] = (
    "generation_dir", "generation_mode", "generated_at", "letter_state",
    "reviewed_at", "review_note",
)

#: v5 -> v6: what normalisation extracts from a posting, stored on the posting.
#: Facts about the job, not about the owner — those live in `job_matches`.
JOB_SEARCH_COLUMNS: tuple[tuple[str, str], ...] = (
    # The employer as the posting names it. Not `company_name`: every query
    # that joins `companies` already selects `c.name AS company_name`.
    ("posting_company", "TEXT"),
    ("target_company_id", "INTEGER"),
    ("company_norm", "TEXT"),
    ("title_norm", "TEXT"),
    ("title_key", "TEXT"),
    ("canonical_url", "TEXT"),
    ("country", "TEXT"),
    ("countries", "TEXT"),
    ("city", "TEXT"),
    ("cities", "TEXT"),
    ("remote_status", "TEXT"),
    ("remote_region", "TEXT"),
    ("employment_types", "TEXT"),
    ("experience_min", "REAL"),
    ("experience_max", "REAL"),
    ("experience_level", "TEXT"),
    ("experience_required", "INTEGER"),
    ("experience_confidence", "REAL"),
    ("experience_evidence", "TEXT"),
    ("education_required", "TEXT"),
    ("education_preferred", "TEXT"),
    ("salary_min", "REAL"),
    ("salary_max", "REAL"),
    ("salary_currency", "TEXT"),
    ("salary_text", "TEXT"),
    ("visa_status", "TEXT"),
    ("visa_evidence", "TEXT"),
    ("visa_scope", "TEXT"),       # ISO2 / EU: where a stated refusal applies
    ("relocation", "INTEGER"),
    ("role_labels", "TEXT"),
    ("skills", "TEXT"),
    ("description_truncated", "INTEGER DEFAULT 0"),
    ("language", "TEXT"),
    ("languages_required", "TEXT"),
    ("content_hash", "TEXT"),
    ("previous_description", "TEXT"),
    ("content_changed_at", "TEXT"),
    ("normalized_hash", "TEXT"),
    ("normalizer_version", "TEXT"),
    ("cluster_id", "INTEGER"),
    ("possible_duplicate_of", "INTEGER"),
)

#: v6 -> v7: what a source states outright, kept so normalisation can use it,
#: and the list form of a remote job's regions.
JOB_SOURCE_FACT_COLUMNS: tuple[tuple[str, str], ...] = (
    #: 1 / 0 when the source says remote or not; NULL when it does not say.
    ("source_remote", "INTEGER"),
    #: ISO2 when the search was country-scoped (an Adzuna DE search is).
    ("source_country", "TEXT"),
    #: The search that last returned this posting — for query yield.
    ("found_query", "TEXT"),
    #: JSON list of ISO2 / EU / EUROPE / EMEA / NA / WORLD. `remote_region`
    #: stays as the first entry for display.
    ("remote_regions", "TEXT"),
)

#: v7 -> v8: grading facts derived at normalisation (Phase 3).
JOB_GRADING_COLUMNS: tuple[tuple[str, str], ...] = (
    #: JSON list: skills named only as wishes ("a plus", "nice to have").
    ("skills_nice", "TEXT"),
    #: 1 when the poster is a recruitment agency hiring for a client.
    ("agency", "INTEGER DEFAULT 0"),
)

#: v5 -> v6: source health (§46).
JOB_SOURCE_HEALTH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("target_company_id", "INTEGER"),
    ("last_success_at", "TEXT"),
    ("last_failure_at", "TEXT"),
    ("last_error", "TEXT"),
    ("jobs_found", "INTEGER"),
    ("jobs_new", "INTEGER"),
    ("jobs_updated", "INTEGER"),
    ("duration_ms", "INTEGER"),
)

_local = threading.local()

#: Process-wide write lock — §1.1.
#:
#: WAL mode makes readers concurrent with a writer. It does **not** make writers
#: concurrent with each other: SQLite still takes an exclusive lock per write
#: transaction, and N threads each holding their own connection is the classic
#: `database is locked` failure that only ever appears under load.
#:
#: The pipeline's first defence is structural — worker threads do network I/O
#: and return their results, and the thread that submitted the work does every
#: write. This lock is the second: the Streamlit UI thread edits CRM rows while
#: a background run is writing, and those two really are different threads.
#:
#: Re-entrant, because a repository write can call another one.
_write_lock = threading.RLock()


@contextmanager
def writing() -> Iterator[None]:
    """Hold the process-wide write lock for a group of statements."""
    with _write_lock:
        yield


# ───────────────────────────── connection ─────────────────────────────


def get_conn(settings: Settings | None = None, *, path: Path | str | None = None) -> sqlite3.Connection:
    """Thread-local connection with row access by name and FKs enforced."""
    settings = settings or get_settings()
    target = Path(path) if path is not None else settings.db_file
    key = str(target)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if key in cache:
        return cache[key]
    if key != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(key, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if key != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    cache[key] = conn
    _local.conns = cache
    return conn


def close_all() -> None:
    for conn in getattr(_local, "conns", {}).values():
        with contextlib.suppress(Exception):  # pragma: no cover
            conn.close()
    _local.conns = {}


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (connections run in autocommit / isolation_level=None).

    Holds the process-wide write lock, so two threads cannot interleave two
    transactions on two connections and collide on SQLite's exclusive lock.
    """
    with _write_lock:
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> int:
    """Create every table/index and record the schema version. Idempotent."""
    conn.executescript(DDL)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    if current < SCHEMA_VERSION:
        _migrate(conn, current)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
        )
    return SCHEMA_VERSION


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """Idempotent `ALTER TABLE … ADD COLUMN`. Returns True if it was added."""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Forward-only migrations, each safe to re-run.

    * v1 -> v2 added `search_results.processed`.
    * v2 -> v3 added `scores.provider` (§0.4), `runs.command` (§0.5) and
      `candidates.template_count` (§1.4).
    * v3 -> v4 added the `jobs` / `job_sources` tables and three `companies`
      columns (§2.2).
    * v4 -> v5 added the four contact-enrichment tables and `quota.reset_at`
      (§6 of PITWALL_V3_EMAIL_ENRICHMENT.md).
    * v5 -> v6 added the job search: `target_companies`, `job_matches`,
      `job_review`, `applications`, `application_events`, the normalised
      columns on `jobs` and the health columns on `job_sources`.
    * v6 -> v7 added `source_queries`, `discovered_boards`, `app_state` and
      four columns on `jobs`: what the source states about remote work and
      country, the search that found the posting, and `remote_regions`.
    * v7 -> v8 added two grading columns on `jobs`: nice-to-have skills and
      the agency flag. (Sponsor registers are applied at scoring time.)
    * v8 -> v9 added `job_review.feedback_reason` and `feedback_note`: why a
      job was rated a bad match.
    * v9 -> v10 added RocketApply's lifecycle: `export_batches`,
      `export_batch_items`, `outreach_notes`, the generation/review columns on
      `applications`, and `job_matches.missing_languages` (languages used to be
      stored in `missing_skills`).

    `CREATE TABLE IF NOT EXISTS` covers a fresh database; an existing 2.0 MB file
    only ever needs the ALTERs, which is why each one is guarded rather than
    versioned. Nothing here drops or rewrites a row.
    """
    _add_column(conn, "search_results", "processed", "INTEGER DEFAULT 0")
    _add_column(conn, "scores", "provider", "TEXT")
    _add_column(conn, "runs", "command", "TEXT")
    _add_column(conn, "candidates", "template_count", "INTEGER DEFAULT 0")
    # v3 -> v4: job openings. The CREATE TABLEs above are IF NOT EXISTS so a
    # fresh database gets them for free; only `companies` needs altering.
    for column in ("careers_url", "ats_kind", "ats_token"):
        _add_column(conn, "companies", column, "TEXT")
    # v4 -> v5: contact enrichment. All four tables are IF NOT EXISTS above, so
    # the only thing an existing file needs is the credit reset date — and it is
    # on `quota` rather than a new table because §6 is explicit that credit
    # accounting reuses `quota` instead of growing a parallel counter.
    _add_column(conn, "quota", "reset_at", "TEXT")
    # v5 -> v6: the job search. New tables are IF NOT EXISTS above; the columns
    # on `jobs` and `job_sources` are added here, and so are their indexes —
    # an index in DDL would run before the column exists on an old file.
    for column, ddl in JOB_SEARCH_COLUMNS:
        _add_column(conn, "jobs", column, ddl)
    for column, ddl in JOB_SOURCE_HEALTH_COLUMNS:
        _add_column(conn, "job_sources", column, ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_company_norm ON jobs(company_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_cluster ON jobs(cluster_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_canonical_url ON jobs(canonical_url)")
    # v6 -> v7: source facts on `jobs`; `source_queries` is IF NOT EXISTS above.
    for column, ddl in JOB_SOURCE_FACT_COLUMNS:
        _add_column(conn, "jobs", column, ddl)
    # v7 -> v8: grading facts.
    for column, ddl in JOB_GRADING_COLUMNS:
        _add_column(conn, "jobs", column, ddl)
    # v8 -> v9: why a job was rated a bad match, in the owner's words.
    _add_column(conn, "job_review", "feedback_reason", "TEXT")
    _add_column(conn, "job_review", "feedback_note", "TEXT")
    # v9 -> v10: what was generated for an application, whether it was reviewed,
    # and the languages a posting asks for that the owner lacks.
    for column in APPLICATION_GENERATION_COLUMNS:
        _add_column(conn, "applications", column, "TEXT")
    _add_column(conn, "job_matches", "missing_languages", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_applications_status ON applications(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_applications_job ON applications(job_id)")


def vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")


def backup_database(conn: sqlite3.Connection, directory: Path | str, *,
                    label: str = "backup") -> Path:
    """Copy the live database to `directory/pitwall-<label>-<time>.db`.

    Uses SQLite's online backup API rather than copying the file: the copy is
    a consistent snapshot that includes what is still in the WAL, and it is
    safe while the app or a pipeline run has the database open.
    """
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"pitwall-{label}-{datetime.now():%Y-%m-%d-%H%M%S}"
    target = folder / f"{stem}.db"
    n = 1
    while target.exists():
        n += 1
        target = folder / f"{stem}-{n}.db"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def loads(value: Any) -> list[str]:
    """Tolerant JSON-array reader for the TEXT columns."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else [str(out)]
    except (json.JSONDecodeError, TypeError):
        return [p.strip() for p in str(value).split("|") if p.strip()]


# ───────────────────────────── repositories ───────────────────────────


class CompanyRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, c: Any) -> int:
        """Upsert by canonical name — editing the CSV updates, never duplicates."""
        self.conn.execute(
            """
            INSERT INTO companies (name, aliases, negative_keywords, linkedin_slug,
                                   domain, series, country, tier, active, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                aliases=excluded.aliases,
                negative_keywords=excluded.negative_keywords,
                linkedin_slug=excluded.linkedin_slug,
                domain=excluded.domain,
                series=excluded.series,
                country=excluded.country,
                tier=excluded.tier,
                active=excluded.active,
                updated_at=datetime('now')
            """,
            (
                c.name,
                _dumps(c.aliases),
                _dumps(c.negative_keywords),
                c.linkedin_slug,
                c.domain,
                c.series,
                c.country,
                c.tier,
                int(c.active),
            ),
        )
        row = self.conn.execute("SELECT id FROM companies WHERE name = ?", (c.name,)).fetchone()
        return int(row["id"])

    def all(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM companies"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY tier ASC, name ASC"
        return [_row_to_company(r) for r in self.conn.execute(sql)]

    def by_name(self, name: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM companies WHERE name = ?", (name,)).fetchone()
        return _row_to_company(r) if r else None

    def deactivate_missing(self, keep_names: Iterable[str]) -> int:
        names = list(keep_names)
        placeholders = ",".join("?" * len(names)) or "''"
        cur = self.conn.execute(
            f"UPDATE companies SET active = 0 WHERE name NOT IN ({placeholders})", names
        )
        return cur.rowcount


class RoleRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, r: Any) -> int:
        self.conn.execute(
            """
            INSERT INTO roles (title, aliases, family, seniority, keywords,
                               priority, active, updated_at)
            VALUES (?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(title) DO UPDATE SET
                aliases=excluded.aliases,
                family=excluded.family,
                seniority=excluded.seniority,
                keywords=excluded.keywords,
                priority=excluded.priority,
                active=excluded.active,
                updated_at=datetime('now')
            """,
            (
                r.title,
                _dumps(r.aliases),
                r.family,
                _dumps(r.seniority),
                _dumps(r.keywords),
                r.priority,
                int(r.active),
            ),
        )
        row = self.conn.execute("SELECT id FROM roles WHERE title = ?", (r.title,)).fetchone()
        return int(row["id"])

    def all(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM roles"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY priority ASC, title ASC"
        return [_row_to_role(r) for r in self.conn.execute(sql)]

    def deactivate_missing(self, keep_titles: Iterable[str]) -> int:
        titles = list(keep_titles)
        placeholders = ",".join("?" * len(titles)) or "''"
        cur = self.conn.execute(
            f"UPDATE roles SET active = 0 WHERE title NOT IN ({placeholders})", titles
        )
        return cur.rowcount


class QueryRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert_many(self, specs: Sequence[Any]) -> tuple[int, int]:
        """Insert planned queries. Returns (inserted, skipped_existing)."""
        inserted = 0
        for q in specs:
            cur = self.conn.execute(
                """
                INSERT INTO queries (company_id, role_id, template_id, query_text,
                                     query_hash, priority, status, provider)
                VALUES (?,?,?,?,?,?,'pending',?)
                ON CONFLICT(query_hash) DO UPDATE SET
                    priority = MAX(queries.priority, excluded.priority)
                """,
                (
                    q.company_id,
                    q.role_id,
                    q.template_id,
                    q.query_text,
                    q.query_hash,
                    q.priority,
                    q.provider,
                ),
            )
            if cur.rowcount == 1 and cur.lastrowid:
                inserted += 1
        return inserted, len(specs) - inserted

    def pending(self, limit: int | None = None, *, stale_before: str | None = None) -> list[dict[str, Any]]:
        """Pending + failed queries, plus `done` ones older than the cache TTL."""
        sql = """
            SELECT q.*, c.name AS company_name, r.title AS role_title
            FROM queries q
            LEFT JOIN companies c ON c.id = q.company_id
            LEFT JOIN roles r     ON r.id = q.role_id
            WHERE q.status IN ('pending','failed')
        """
        params: list[Any] = []
        if stale_before:
            sql += " OR (q.status = 'done' AND (q.last_run_at IS NULL OR q.last_run_at < ?))"
            params.append(stale_before)
        sql += " ORDER BY q.priority DESC, q.id ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def mark(
        self,
        query_id: int,
        status: str,
        *,
        provider: str | None = None,
        results_count: int | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE queries
               SET status = ?,
                   provider = COALESCE(?, provider),
                   results_count = COALESCE(?, results_count),
                   error = ?,
                   last_run_at = datetime('now')
             WHERE id = ?
            """,
            (status, provider, results_count, error, query_id),
        )

    def counts_by_status(self) -> dict[str, int]:
        return {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) n FROM queries GROUP BY status")
        }

    def reset_running(self) -> int:
        cur = self.conn.execute("UPDATE queries SET status='pending' WHERE status='running'")
        return cur.rowcount


class ResultRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert_hits(self, query_id: int, provider: str, hits: Sequence[Any]) -> int:
        from .extract.linkedin import clean_serp_title
        from .extract.normalize import canonicalize_url

        n = 0
        for h in hits:
            canon = canonicalize_url(h.url)
            cur = self.conn.execute(
                """
                INSERT INTO search_results
                       (query_id, provider, rank, url, canonical_url, title, snippet, raw_json)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(query_id, canonical_url) DO NOTHING
                """,
                (
                    query_id,
                    provider,
                    h.rank,
                    h.url,
                    canon,
                    clean_serp_title(h.title),
                    h.snippet,
                    json.dumps(h.raw, ensure_ascii=False)[:20000],
                ),
            )
            n += cur.rowcount or 0
        return n

    def unprocessed(self, limit: int | None = None, *, reprocess: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM search_results"
        if not reprocess:
            sql += " WHERE processed = 0"
        sql += " ORDER BY id ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql)]

    def mark_processed(self, ids: Sequence[int]) -> None:
        self.conn.executemany(
            "UPDATE search_results SET processed = 1 WHERE id = ?", [(i,) for i in ids]
        )

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) n FROM search_results").fetchone()["n"])


class CandidateRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def is_blocked(self, slug: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM blocklist WHERE linkedin_slug = ?", (slug,)
            ).fetchone()
            is not None
        )

    def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM candidates WHERE linkedin_slug = ?", (slug,)
        ).fetchone()
        return dict(r) if r else None

    def upsert(self, fields: dict[str, Any], *, replace: bool = False) -> tuple[int, bool]:
        """Insert or merge a candidate. Returns (candidate_id, is_new).

        `replace=True` (used by `extract --reprocess`) takes the fresh parse
        wholesale instead of merging: longest-wins would otherwise preserve a
        long, wrong value that a parser fix has since corrected.
        """
        slug = fields["linkedin_slug"]
        existing = self.get_by_slug(slug)
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO candidates (linkedin_slug, profile_url, full_name, headline,
                                        current_title, current_company, location)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    slug,
                    fields.get("profile_url", f"https://www.linkedin.com/in/{slug}"),
                    fields.get("full_name"),
                    fields.get("headline"),
                    fields.get("current_title"),
                    fields.get("current_company"),
                    fields.get("location"),
                ),
            )
            return int(cur.lastrowid or 0), True

        from .extract.dedupe import merge_fields

        merged = {**existing, **{k: v for k, v in fields.items() if v}} if replace else merge_fields(
            existing, fields
        )
        self.conn.execute(
            """
            UPDATE candidates
               SET profile_url = ?, full_name = ?, headline = ?, current_title = ?,
                   current_company = ?, location = ?, last_seen_at = datetime('now')
             WHERE id = ?
            """,
            (
                merged.get("profile_url"),
                merged.get("full_name"),
                merged.get("headline"),
                merged.get("current_title"),
                merged.get("current_company"),
                merged.get("location"),
                existing["id"],
            ),
        )
        return int(existing["id"]), False

    def link_source(self, candidate_id: int, search_result_id: int) -> bool:
        cur = self.conn.execute(
            """
            INSERT INTO candidate_sources (candidate_id, search_result_id)
            VALUES (?,?) ON CONFLICT DO NOTHING
            """,
            (candidate_id, search_result_id),
        )
        return bool(cur.rowcount)

    def refresh_evidence_count(self, candidate_id: int) -> int:
        """How many distinct queries — and distinct TEMPLATES — surfaced this person.

        The two are different signals. Two near-identical queries finding the
        same profile says little; `profile_exact` and `github_discovery` both
        finding them is real corroboration, because the two look at different
        corners of the web (§1.4).
        """
        row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT sr.query_id)   AS queries,
                   COUNT(DISTINCT q.template_id) AS templates
              FROM candidate_sources cs
              JOIN search_results sr ON sr.id = cs.search_result_id
              LEFT JOIN queries q    ON q.id = sr.query_id
             WHERE cs.candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE candidates SET evidence_count = ?, template_count = ? WHERE id = ?",
            (row["queries"], row["templates"], candidate_id),
        )
        return int(row["queries"])

    def evidence(self, candidate_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT sr.id AS search_result_id, sr.query_id, sr.title, sr.snippet,
                       sr.url, sr.rank, sr.provider, sr.fetched_at,
                       q.query_text, q.template_id,
                       c.name AS company_name, r.title AS role_title
                  FROM candidate_sources cs
                  JOIN search_results sr ON sr.id = cs.search_result_id
                  LEFT JOIN queries q    ON q.id = sr.query_id
                  LEFT JOIN companies c  ON c.id = q.company_id
                  LEFT JOIN roles r      ON r.id = q.role_id
                 WHERE cs.candidate_id = ?
                 ORDER BY sr.rank ASC, sr.id ASC
                """,
                (candidate_id,),
            )
        ]

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM candidates ORDER BY id")]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"])

    def delete(self, candidate_id: int) -> None:
        self.conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))


class ScoreRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, s: Any) -> int:
        self.conn.execute(
            """
            INSERT INTO scores (candidate_id, company_id, role_id, heuristic_score,
                                llm_score, final_score, verdict, is_current_employee,
                                company_match, role_match, seniority, confidence,
                                reasons, red_flags, model, provider, prompt_version,
                                scored_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(candidate_id, company_id, role_id, prompt_version) DO UPDATE SET
                heuristic_score=excluded.heuristic_score,
                llm_score=excluded.llm_score,
                final_score=excluded.final_score,
                verdict=excluded.verdict,
                is_current_employee=excluded.is_current_employee,
                company_match=excluded.company_match,
                role_match=excluded.role_match,
                seniority=excluded.seniority,
                confidence=excluded.confidence,
                reasons=excluded.reasons,
                red_flags=excluded.red_flags,
                model=excluded.model,
                provider=excluded.provider,
                scored_at=datetime('now')
            """,
            (
                s.candidate_id,
                s.company_id,
                s.role_id,
                s.heuristic_score,
                s.llm_score,
                s.final_score,
                s.verdict,
                None if s.is_current_employee is None else int(s.is_current_employee),
                s.company_match,
                s.role_match,
                s.seniority,
                s.confidence,
                _dumps(s.reasons),
                _dumps(s.red_flags),
                s.model,
                getattr(s, "provider", None),
                s.prompt_version,
            ),
        )
        row = self.conn.execute(
            """
            SELECT id FROM scores
             WHERE candidate_id = ? AND company_id IS ? AND role_id IS ? AND prompt_version = ?
            """,
            (s.candidate_id, s.company_id, s.role_id, s.prompt_version),
        ).fetchone()
        return int(row["id"]) if row else 0

    def for_candidate(self, candidate_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT s.*, c.name AS company_name, r.title AS role_title
                  FROM scores s
                  LEFT JOIN companies c ON c.id = s.company_id
                  LEFT JOIN roles r     ON r.id = s.role_id
                 WHERE s.candidate_id = ?
                 ORDER BY s.final_score DESC
                """,
                (candidate_id,),
            )
        ]

    #: The three states that mean "no AI has read this person yet": a run that
    #: never reached them, a budget that passed over them (§1.2), and an answer
    #: that came back unreadable. All three are still candidates.
    AWAITING_LLM = ("pending_llm", "needs_review", "skipped_triage")

    @staticmethod
    def _candidate_filter(candidate_ids: Sequence[int] | None) -> tuple[str, list[Any]]:
        """An optional `AND s.candidate_id IN (…)`, as SQL plus its parameters.

        Through `json_each` rather than a placeholder per id: the People page
        can hand this a selection of many hundreds, and one JSON parameter has
        no variable-count ceiling to trip over.
        """
        if candidate_ids is None:
            return "", []
        ids = [int(i) for i in candidate_ids]
        return " AND s.candidate_id IN (SELECT value FROM json_each(?))", [json.dumps(ids)]

    def awaiting_llm(
        self,
        prompt_version: str,
        limit: int | None = None,
        candidate_ids: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Who the LLM stage would read next, best heuristic score first.

        `candidate_ids` narrows it to a chosen few — what the People page's
        "analyse these people" button runs on. An empty sequence means exactly
        that: nobody was chosen, so nothing is returned (`None` means no filter).
        """
        where, args = self._candidate_filter(candidate_ids)
        sql = f"""
            SELECT s.*, c.name AS company_name, r.title AS role_title
              FROM scores s
              LEFT JOIN companies c ON c.id = s.company_id
              LEFT JOIN roles r     ON r.id = s.role_id
             WHERE s.prompt_version = ?
               AND s.llm_score IS NULL
               -- `skipped_triage` is included on purpose: a candidate the last
               -- run chose not to spend a verdict on is still a candidate. A
               -- lower bar, or a bigger budget, picks it up next time (§1.2).
               AND s.verdict IN {self.AWAITING_LLM}
               {where}
             ORDER BY s.heuristic_score DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, (prompt_version, *args))]

    def awaiting_llm_counts(
        self, prompt_version: str, candidate_ids: Sequence[int] | None = None
    ) -> dict[str, int]:
        """How many candidates `awaiting_llm` would return, split by verdict.

        The same WHERE clause, counted rather than read, so the "resume the
        analysis" controls can say how much work is left without pulling every
        row into the browser process. The split matters: waiting for AI and
        skipped to save budget are different facts (§1.2), and the keys here are
        the verdicts the UI already has words for.
        """
        where, args = self._candidate_filter(candidate_ids)
        rows = self.conn.execute(
            f"""
            SELECT s.verdict, COUNT(*) AS n
              FROM scores s
             WHERE s.prompt_version = ?
               AND s.llm_score IS NULL
               AND s.verdict IN {self.AWAITING_LLM}
               {where}
             GROUP BY s.verdict
            """,
            (prompt_version, *args),
        ).fetchall()
        return {str(r["verdict"]): int(r["n"]) for r in rows}

    def awaiting_llm_candidates(
        self, prompt_version: str, candidate_ids: Sequence[int] | None = None
    ) -> list[int]:
        """The distinct people awaiting a verdict — one id each, best first.

        `awaiting_llm` returns score ROWS, and one person can hold several (one
        per company/role pair they were matched against). What the People page
        counts and ticks is people, so it asks for them this way.
        """
        where, args = self._candidate_filter(candidate_ids)
        rows = self.conn.execute(
            f"""
            SELECT s.candidate_id, MAX(s.heuristic_score) AS best
              FROM scores s
             WHERE s.prompt_version = ?
               AND s.llm_score IS NULL
               AND s.verdict IN {self.AWAITING_LLM}
               {where}
             GROUP BY s.candidate_id
             ORDER BY best DESC
            """,
            (prompt_version, *args),
        ).fetchall()
        return [int(r["candidate_id"]) for r in rows]

    def mark_triaged(self, ids: Sequence[int]) -> int:
        """Mark score rows as deliberately not sent to the LLM (§1.2)."""
        if not ids:
            return 0
        self.conn.executemany(
            "UPDATE scores SET verdict = 'skipped_triage' WHERE id = ?"
            " AND verdict IN ('pending_llm','needs_review','skipped_triage')",
            [(int(i),) for i in ids],
        )
        return len(ids)

    def delete_for_version(self, prompt_version: str) -> int:
        cur = self.conn.execute("DELETE FROM scores WHERE prompt_version = ?", (prompt_version,))
        return cur.rowcount

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"])


class CRMRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def ensure(self, candidate_id: int) -> dict[str, Any]:
        self.conn.execute(
            "INSERT INTO crm (candidate_id) VALUES (?) ON CONFLICT DO NOTHING", (candidate_id,)
        )
        return self.get(candidate_id)  # type: ignore[return-value]

    def get(self, candidate_id: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM crm WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = loads(d.get("tags"))
        return d

    def update(self, candidate_id: int, **fields: Any) -> None:
        if not fields:
            return
        self.ensure(candidate_id)
        if "tags" in fields:
            fields["tags"] = _dumps(fields["tags"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE crm SET {sets}, updated_at = datetime('now') WHERE candidate_id = ?",
            (*fields.values(), candidate_id),
        )

    def block(self, slug: str, reason: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO blocklist (linkedin_slug, reason) VALUES (?,?) ON CONFLICT DO NOTHING",
            (slug, reason),
        )

    def unblock(self, slug: str) -> None:
        self.conn.execute("DELETE FROM blocklist WHERE linkedin_slug = ?", (slug,))

    def blocklist(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM blocklist ORDER BY created_at DESC")]

    def counts_by_status(self) -> dict[str, int]:
        return {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) n FROM crm GROUP BY status")
        }


#: Run kinds, by the app that owns them. PitWall's "is anything running?" asks
#: only about its own, so a RocketApply job search never shows as a PitWall run
#: (or gets stopped by PitWall's Stop button), and the other way round.
NETWORKING_COMMANDS = ("run", "resume-llm", "search", "extract", "score", "jobs",
                       "enrich", "enrich-hunter")
JOB_SEARCH_COMMANDS = ("job-search",)


class RunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(self, params: dict[str, Any], command: str = "run") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (command, params, status) VALUES (?, ?, 'running')",
            (command, json.dumps(params, ensure_ascii=False, default=str)),
        )
        run_id = int(cur.lastrowid or 0)
        self.conn.execute(
            "INSERT INTO run_progress (run_id, stage, message) VALUES (?, 'start', 'queued')"
            " ON CONFLICT DO NOTHING",
            (run_id,),
        )
        return run_id

    #: How long a `running` row may go without a progress heartbeat before it is
    #: assumed to belong to a process that is gone. Generously above the longest
    #: gap a live run can leave: an LLM stage waiting out a rate limit reports
    #: every 90 seconds at the very worst.
    STALE_AFTER_MINUTES = 30.0

    def reset_stale(self, idle_minutes: float | None = None, keep: int | None = None) -> int:
        """Close run rows left at `running` by a process that never came back.

        A crash, a `kill`, or a laptop lid: SIGTERM does not raise in Python, so
        `tracked()`'s `finally` never runs and the row stays open for ever. The
        next run sweeps them, so history shows `interrupted` rather than a
        handful of jobs that look like they are still going.

        Staleness is measured by the progress heartbeat, never by "an older row
        exists". Two runs can legitimately overlap — the app can start one while
        the CLI is mid-sweep — and closing the older one because a newer one
        began marked a *live* run as interrupted, which is how a run that was
        still scoring people appeared to have died.
        """
        idle = self.STALE_AFTER_MINUTES if idle_minutes is None else float(idle_minutes)
        sql = """
            UPDATE runs SET status = 'interrupted', finished_at = datetime('now'),
                   error = COALESCE(error, 'process ended without closing this run')
             WHERE status = 'running'
               AND COALESCE(
                     (SELECT p.updated_at FROM run_progress p WHERE p.run_id = runs.id),
                     started_at
                   ) < datetime('now', ?)
        """
        args: list[Any] = [f"-{idle:g} minutes"]
        if keep is not None:
            sql += " AND id <> ?"
            args.append(int(keep))
        return self.conn.execute(sql, args).rowcount or 0

    def active(self, idle_minutes: float = 2.0, *,
               commands: Sequence[str] | None = None) -> dict[str, Any] | None:
        """The run that is going on right now, if there is one.

        "Running, and it said something recently." The heartbeat is what makes
        this safe to act on: a row abandoned by a killed process stops updating
        and stops counting as active, so one crash cannot lock the button that
        starts the next run for ever.

        `commands` narrows it to runs of those kinds — RocketApply asks only
        about `job-search`, so a people search in PitWall does not block
        "Fetch new jobs", and the other way round. None = any run, as before.
        """
        kinds = ""
        params: list[Any] = [f"-{float(idle_minutes):g} minutes"]
        if commands:
            kinds = f" AND r.command IN ({','.join('?' * len(commands))})"
            params += list(commands)
        row = self.conn.execute(
            f"""
            SELECT r.*, p.stage, p.message, p.current, p.total, p.updated_at AS heartbeat
              FROM runs r
              LEFT JOIN run_progress p ON p.run_id = r.id
             WHERE r.status = 'running'
               AND COALESCE(p.updated_at, r.started_at) >= datetime('now', ?){kinds}
             ORDER BY r.id DESC LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None

    def bump(self, run_id: int, **counters: int | float) -> None:
        if not counters:
            return
        sets = ", ".join(f"{k} = COALESCE({k},0) + ?" for k in counters)
        self.conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*counters.values(), run_id))

    def finish(self, run_id: int, status: str = "done", error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = datetime('now'), status = ?, error = ? WHERE id = ?",
            (status, error, run_id),
        )

    def progress(self, run_id: int, stage: str, message: str, current: int = 0, total: int = 0) -> None:
        self.conn.execute(
            """
            INSERT INTO run_progress (run_id, stage, message, current, total, updated_at)
            VALUES (?,?,?,?,?, datetime('now'))
            ON CONFLICT(run_id) DO UPDATE SET
                stage=excluded.stage, message=excluded.message,
                current=excluded.current, total=excluded.total,
                updated_at=datetime('now')
            """,
            (run_id, stage, message, current, total),
        )
        self._record_event(run_id, stage, message, current, total)

    def _record_event(
        self, run_id: int, stage: str, message: str, current: int, total: int
    ) -> None:
        """Append on a stage change, update in place within a stage.

        Progress fires every few queries; one row per call would be thousands of
        rows saying the same thing. One row per stage, carrying that stage's
        latest message and counts, is the timeline the UI actually needs.
        """
        last = self.conn.execute(
            "SELECT id, stage FROM run_events WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if last is not None and last["stage"] == stage:
            self.conn.execute(
                "UPDATE run_events SET message = ?, current = ?, total = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (message, current, total, last["id"]),
            )
            return
        self.conn.execute(
            "INSERT INTO run_events (run_id, stage, message, current, total)"
            " VALUES (?,?,?,?,?)",
            (run_id, stage, message, current, total),
        )

    def events(self, run_id: int) -> list[dict[str, Any]]:
        """The stage-by-stage timeline of one run, oldest first."""
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
            )
        ]

    def get_progress(self, run_id: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM run_progress WHERE run_id = ?", (run_id,)).fetchone()
        return dict(r) if r else None

    def cancel(self, run_id: int) -> None:
        self.conn.execute("UPDATE run_progress SET cancelled = 1 WHERE run_id = ?", (run_id,))

    def is_cancelled(self, run_id: int) -> bool:
        r = self.conn.execute(
            "SELECT cancelled FROM run_progress WHERE run_id = ?", (run_id,)
        ).fetchone()
        return bool(r and r["cancelled"])

    def latest(self, n: int = 10, *,
               commands: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """The newest `n` runs; `commands` narrows them to those kinds (None = any)."""
        kinds, params = "", []
        if commands:
            kinds = f" WHERE command IN ({','.join('?' * len(commands))})"
            params = list(commands)
        sql = f"SELECT * FROM runs{kinds} ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, (*params, n))]

    def get(self, run_id: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None


class QuotaRepo:
    """Free-tier guards — §13.2."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def used_today(self, provider: str) -> int:
        r = self.conn.execute(
            "SELECT requests FROM quota WHERE provider = ? AND day = ?", (provider, today_utc())
        ).fetchone()
        return int(r["requests"]) if r else 0

    def used_since(self, provider: str, days: int) -> int:
        """Requests over the last `days` UTC days, today included.

        A rolling window, because a provider that states "1,000 a week" rarely
        says which day its week starts on, and a rolling count can never be
        over the limit under any choice of start day.
        """
        r = self.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) AS n FROM quota"
            " WHERE provider = ? AND day >= date(?, ?)",
            (provider, today_utc(), f"-{max(0, int(days) - 1)} days"),
        ).fetchone()
        return int(r["n"]) if r else 0

    def consume(self, provider: str, n: int = 1) -> int:
        self.conn.execute(
            """
            INSERT INTO quota (provider, day, requests) VALUES (?,?,?)
            ON CONFLICT(provider, day) DO UPDATE SET requests = quota.requests + excluded.requests
            """,
            (provider, today_utc(), n),
        )
        return self.used_today(provider)

    def credits(self, provider: str) -> int | None:
        r = self.conn.execute(
            "SELECT credits FROM quota WHERE provider = ? AND credits IS NOT NULL"
            " ORDER BY day DESC LIMIT 1",
            (provider,),
        ).fetchone()
        return int(r["credits"]) if r and r["credits"] is not None else None

    def set_credits(self, provider: str, credits: int, *, reset_at: str | None = None) -> None:
        """Record remaining credits, and optionally when the allowance refills.

        `reset_at` is only ever written when the caller has one — passing None
        leaves whatever is already stored, so a cheap local decrement after a
        spend does not erase the date the provider itself reported.
        """
        self.conn.execute(
            """
            INSERT INTO quota (provider, day, requests, credits, reset_at) VALUES (?,?,0,?,?)
            ON CONFLICT(provider, day) DO UPDATE SET
                credits  = excluded.credits,
                reset_at = COALESCE(excluded.reset_at, quota.reset_at)
            """,
            (provider, today_utc(), max(0, credits), reset_at),
        )

    def reset_at(self, provider: str) -> str | None:
        r = self.conn.execute(
            "SELECT reset_at FROM quota WHERE provider = ? AND reset_at IS NOT NULL"
            " ORDER BY day DESC LIMIT 1",
            (provider,),
        ).fetchone()
        return str(r["reset_at"]) if r and r["reset_at"] else None

    def credit_state(self, provider: str) -> dict[str, Any]:
        """`{credits, reset_at, used_today}` — one read for the whole sidebar."""
        return {
            "credits": self.credits(provider),
            "reset_at": self.reset_at(provider),
            "used_today": self.used_today(provider),
        }

    def spend_credits(self, provider: str, n: float = 1) -> int | None:
        """Optimistic local decrement between two polls of the provider.

        Rounds the *remainder* down, never up: Hunter's verifier costs half a
        credit, and a counter that rounded 49.5 to 50 would offer the owner a
        spend the provider is about to refuse. The provider's own number always
        wins on the next poll; this only has to avoid over-promising until then.
        """
        cur = self.credits(provider)
        if cur is None:
            return None
        new = int(math.floor(max(0.0, cur - float(n))))
        self.set_credits(provider, new)
        return new

    def history(self, days: int = 30) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM quota ORDER BY day DESC, provider LIMIT ?", (days * 6,)
            )
        ]


class JobSourceRepo:
    """Where postings come from, and whether that source is healthy (§2.3)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, name: str, kind: str, base_url: str | None = None,
               company_id: int | None = None, enabled: bool = True,
               target_company_id: int | None = None) -> int:
        """Register a source. The company links only ever fill in.

        One board can be polled by both the Openings run (which knows its
        people-search company) and the job search (which knows its target
        company). Each passes only the link it knows, so an unconditional
        overwrite would have each run erase the other's.
        """
        self.conn.execute(
            """
            INSERT INTO job_sources (name, kind, base_url, company_id, enabled, target_company_id)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                kind=excluded.kind, base_url=excluded.base_url,
                company_id=COALESCE(excluded.company_id, job_sources.company_id),
                target_company_id=COALESCE(excluded.target_company_id,
                                           job_sources.target_company_id),
                enabled=excluded.enabled
            """,
            (name, kind, base_url, company_id, int(enabled), target_company_id),
        )
        row = self.conn.execute("SELECT id FROM job_sources WHERE name = ?", (name,)).fetchone()
        return int(row["id"])

    def mark_polled(self, source_id: int, status: str) -> None:
        """A failing adapter is non-fatal — it is recorded and the run goes on."""
        self.conn.execute(
            "UPDATE job_sources SET last_polled_at = datetime('now'), last_status = ?"
            " WHERE id = ?",
            (status[:200], source_id),
        )

    def record_poll(self, source_id: int, *, ok: bool, error: str | None = None,
                    found: int = 0, new: int = 0, updated: int = 0,
                    duration_ms: int = 0) -> None:
        """§46: enough to tell a healthy source from a silently dead one.

        `last_status` keeps its v4 meaning ('ok' or the error) so the Openings
        page reads it unchanged.
        """
        if ok:
            self.conn.execute(
                """
                UPDATE job_sources
                   SET last_polled_at = datetime('now'), last_status = 'ok',
                       last_success_at = datetime('now'), last_error = NULL,
                       jobs_found = ?, jobs_new = ?, jobs_updated = ?, duration_ms = ?
                 WHERE id = ?
                """,
                (found, new, updated, duration_ms, source_id),
            )
        else:
            message = (error or "failed")[:300]
            self.conn.execute(
                """
                UPDATE job_sources
                   SET last_polled_at = datetime('now'), last_status = ?,
                       last_failure_at = datetime('now'), last_error = ?,
                       jobs_found = 0, jobs_new = 0, jobs_updated = 0, duration_ms = ?
                 WHERE id = ?
                """,
                (message[:200], message, duration_ms, source_id),
            )

    def all(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM job_sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY kind, name")]

    def get(self, name: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM job_sources WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None


def content_hash(title: Any, description: Any, location: Any) -> str:
    """What counts as "the posting changed" (§42): the words a person reads.

    Whitespace is collapsed first, so a board re-rendering the same text with
    different line breaks does not announce a change nobody could see.
    """
    import hashlib

    text = " ".join(str(v or "") for v in (title, location, description))
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()


class JobUpsert(NamedTuple):
    """What `JobRepo.upsert` did — indexable like the v4 `(job_id, is_new)`."""

    job_id: int
    is_new: bool
    changed: bool = False


class JobRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, source_id: int, job: Any, company_id: int | None = None,
               target_company_id: int | None = None) -> JobUpsert:
        """Insert or refresh one posting. Returns `(job_id, is_new, changed)`.

        `last_seen_at` is bumped on every sighting; that timestamp plus a status
        transition is how a closed posting is detected without deleting it.

        `changed` is True when the text a person reads differs from last time
        (§42). The previous description is kept — one generation, enough for
        the UI to show what moved — and `content_changed_at` is stamped.
        """
        existing = self.conn.execute(
            "SELECT id, content_hash, description FROM jobs WHERE source_id = ? AND url = ?",
            (source_id, job.url),
        ).fetchone()
        digest = content_hash(job.title, job.description, job.location)
        raw = json.dumps(job.raw, ensure_ascii=False, default=str)[:40000]
        remote = getattr(job, "remote", None)
        extra = (
            getattr(job, "salary_min", None), getattr(job, "salary_max", None),
            getattr(job, "salary_currency", None),
            int(bool(getattr(job, "description_truncated", False))),
            getattr(job, "company_name", None),
            None if remote is None else int(bool(remote)),
            getattr(job, "country_hint", None),
            getattr(job, "found_query", None),
        )
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO jobs (company_id, target_company_id, source_id, external_id, title,
                                  description, location, employment_type, url, posted_at,
                                  raw_json, content_hash, salary_min, salary_max,
                                  salary_currency, description_truncated, posting_company,
                                  source_remote, source_country, found_query,
                                  status, first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', datetime('now'),
                        datetime('now'))
                """,
                (company_id, target_company_id, source_id, job.external_id, job.title,
                 job.description, job.location, job.employment_type, job.url, job.posted_at,
                 raw, digest, *extra),
            )
            return JobUpsert(int(cur.lastrowid or 0), True, False)
        # A row from before v6 has no hash; filling it in is not a change.
        changed = bool(existing["content_hash"]) and existing["content_hash"] != digest
        self.conn.execute(
            f"""
            UPDATE jobs
               SET company_id = COALESCE(?, company_id),
                   target_company_id = COALESCE(?, target_company_id),
                   title = ?, description = ?,
                   location = ?, employment_type = ?, posted_at = COALESCE(?, posted_at),
                   raw_json = ?, content_hash = ?,
                   salary_min = COALESCE(?, salary_min), salary_max = COALESCE(?, salary_max),
                   salary_currency = COALESCE(?, salary_currency), description_truncated = ?,
                   posting_company = COALESCE(?, posting_company),
                   source_remote = COALESCE(?, source_remote),
                   source_country = COALESCE(?, source_country),
                   found_query = COALESCE(?, found_query),
                   status = 'open', last_seen_at = datetime('now')
                   {", previous_description = ?, content_changed_at = datetime('now')"
                    if changed else ""}
             WHERE id = ?
            """,
            (company_id, target_company_id, job.title, job.description, job.location,
             job.employment_type, job.posted_at, raw, digest, *extra,
             *((existing["description"],) if changed else ()), existing["id"]),
        )
        return JobUpsert(int(existing["id"]), False, changed)

    def close_stale(self, source_id: int, days: int) -> int:
        """Close what a PARTIAL source has not shown for `days` (§43).

        The counterpart of `close_missing` for searches, capped feeds and
        sitemaps: one fetch cannot prove a posting gone, but a week of fetches
        that never return it is a reasonable sign. Closed, never deleted.
        """
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'closed' WHERE source_id = ? AND status = 'open'"
            " AND last_seen_at < datetime('now', ?)",
            (source_id, f"-{int(days)} days"),
        )
        return cur.rowcount or 0

    def close_missing(self, source_id: int, seen_ids: Sequence[int]) -> int:
        """Mark postings this source stopped returning as `closed`.

        Not deleted. A vanished posting is the record of an opportunity that has
        gone, and "you missed this one" is worth more than a tidy table
        (§2.2; Appendix C forbids deleting job history outright).
        """
        keep = ",".join(str(int(i)) for i in seen_ids) or "0"
        cur = self.conn.execute(
            f"UPDATE jobs SET status = 'closed' WHERE source_id = ? AND status = 'open'"
            f" AND id NOT IN ({keep})",
            (source_id,),
        )
        return cur.rowcount or 0

    def open_jobs(self, *, company_id: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT j.*, c.name AS company_name, s.name AS source_name, s.kind AS source_kind
              FROM jobs j
              LEFT JOIN companies c   ON c.id = j.company_id
              LEFT JOIN job_sources s ON s.id = j.source_id
             WHERE j.status = 'open'
        """
        args: list[Any] = []
        if company_id is not None:
            sql += " AND j.company_id = ?"
            args.append(company_id)
        sql += " ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def since(self, iso_timestamp: str) -> list[dict[str, Any]]:
        """Postings first seen after `iso_timestamp` — the Home "new" panel."""
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT j.*, c.name AS company_name
                  FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
                 WHERE j.first_seen_at > ? AND j.status = 'open'
                 ORDER BY j.first_seen_at DESC
                """,
                (iso_timestamp,),
            )
        ]

    def set_relevance(self, job_id: int, score: float | None, reasons: list[str],
                      model: str | None, prompt_version: str | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET relevance_score = ?, relevance_reasons = ?,"
            " relevance_model = ?, relevance_prompt_version = ? WHERE id = ?",
            (score, _dumps(reasons), model, prompt_version, job_id),
        )

    def awaiting_relevance(self, prompt_version: str, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT j.*, c.name AS company_name FROM jobs j
              LEFT JOIN companies c ON c.id = j.company_id
             WHERE j.status = 'open'
               AND (j.relevance_score IS NULL OR j.relevance_prompt_version IS NOT ?)
             ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, (prompt_version,))]

    def counts_by_status(self) -> dict[str, int]:
        return {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
        }

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"])


# ─────────────────────────── row adapters ───────────────────────────


class CompanyDomainRepo:
    """Domains and their inferred address format — §2.1/§2.3 of the v3 brief.

    Company-scoped on purpose. The expensive part of finding one person's
    address is learning their employer's format, and that answer is reusable:
    resolve Alpine's domain and pattern once and every later Alpine candidate is
    free. This table is where that leverage lives.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, company_id: int, domain: str, source: str | None = None) -> int:
        """Record a domain for a company. Never downgrades a manual override."""
        domain = (domain or "").strip().lower().lstrip("@")
        if not domain:
            raise ValueError("a company domain cannot be empty")
        self.conn.execute(
            """
            INSERT INTO company_domains (company_id, domain, source)
            VALUES (?,?,?)
            ON CONFLICT(company_id, domain) DO UPDATE SET
                source = COALESCE(company_domains.source, excluded.source)
            """,
            (company_id, domain, source),
        )
        row = self.conn.execute(
            "SELECT id FROM company_domains WHERE company_id = ? AND domain = ?",
            (company_id, domain),
        ).fetchone()
        return int(row["id"])

    def get(self, company_id: int, domain: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM company_domains WHERE company_id = ? AND domain = ?",
            (company_id, (domain or "").strip().lower().lstrip("@")),
        ).fetchone()
        return dict(r) if r else None

    def by_id(self, domain_id: int) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM company_domains WHERE id = ?", (domain_id,)
        ).fetchone()
        return dict(r) if r else None

    def for_company(self, company_id: int) -> list[dict[str, Any]]:
        """Every known domain, the owner's override first."""
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM company_domains WHERE company_id = ?"
                " ORDER BY manual_override DESC, id ASC",
                (company_id,),
            )
        ]

    def preferred(self, company_id: int) -> dict[str, Any] | None:
        rows = self.for_company(company_id)
        return rows[0] if rows else None

    def set_override(self, company_id: int, domain: str) -> int:
        """The owner's correction. Exactly one override per company."""
        domain_id = self.upsert(company_id, domain, source="manual")
        self.conn.execute(
            "UPDATE company_domains SET manual_override = 0 WHERE company_id = ?",
            (company_id,),
        )
        self.conn.execute(
            "UPDATE company_domains SET manual_override = 1, source = 'manual' WHERE id = ?",
            (domain_id,),
        )
        return domain_id

    def set_pattern(
        self, domain_id: int, pattern: str | None, sample_size: int, agreement: float | None
    ) -> None:
        self.conn.execute(
            """
            UPDATE company_domains
               SET pattern = ?, pattern_sample_size = ?, pattern_agreement = ?
             WHERE id = ?
            """,
            (pattern, int(sample_size), agreement, domain_id),
        )

    def mark_harvested(self, domain_id: int) -> None:
        self.conn.execute(
            "UPDATE company_domains SET last_harvested_at = datetime('now') WHERE id = ?",
            (domain_id,),
        )

    # ── the sample corpus ──

    def add_sample(
        self,
        domain_id: int,
        email: str,
        *,
        person_name: str | None = None,
        source_url: str | None = None,
        source: str = "harvest",
    ) -> bool:
        """Store one known address at this domain. True if it was new.

        "New" is answered by looking first, not by reading `rowcount` — an
        upsert reports 1 for both an insert and an update, and the callers use
        this return value to count what a harvest actually gained.
        """
        email = (email or "").strip().lower()
        if not email:
            return False
        known = self.conn.execute(
            "SELECT 1 FROM domain_address_samples WHERE company_domain_id = ? AND email = ?",
            (domain_id, email),
        ).fetchone() is not None
        self.conn.execute(
            """
            INSERT INTO domain_address_samples
                   (company_domain_id, email, person_name, source_url, source)
            VALUES (?,?,?,?,?)
            ON CONFLICT(company_domain_id, email) DO UPDATE SET
                person_name = COALESCE(domain_address_samples.person_name, excluded.person_name),
                source_url  = COALESCE(domain_address_samples.source_url, excluded.source_url)
            """,
            (domain_id, email, person_name, source_url, source),
        )
        return not known

    def samples(self, domain_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM domain_address_samples WHERE company_domain_id = ?"
                " ORDER BY id ASC",
                (domain_id,),
            )
        ]

    def sample_count(self, domain_id: int) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) n FROM domain_address_samples WHERE company_domain_id = ?",
            (domain_id,),
        ).fetchone()
        return int(r["n"])


class ContactRepo:
    """`contact_emails` — one row per (person, address) — §6 of the v3 brief."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(
        self,
        candidate_id: int,
        email: str,
        *,
        status: str,
        source: str,
        evidence: dict[str, Any] | None = None,
        confidence: float | None = None,
        mx_valid: bool | None = None,
        is_role_address: bool = False,
        is_disposable: bool = False,
        raw: dict[str, Any] | None = None,
    ) -> int:
        """Store a finding, never downgrading one that is already better.

        Statuses are ordered by evidence, so a later pattern guess must not
        overwrite an address that a corroboration search already proved. The
        ordering is enforced here rather than at each caller because every step
        in the chain writes through this one method.
        """
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("a contact row needs an address")
        existing = self.get(candidate_id, email)
        if existing and STATUS_RANK.get(str(existing["status"]), 0) > STATUS_RANK.get(status, 0):
            return int(existing["id"])
        self.conn.execute(
            """
            INSERT INTO contact_emails
                   (candidate_id, email, status, source, evidence_json, confidence,
                    mx_valid, is_role_address, is_disposable, retrieved_at, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?, datetime('now'), ?)
            ON CONFLICT(candidate_id, email) DO UPDATE SET
                status          = excluded.status,
                source          = excluded.source,
                evidence_json   = excluded.evidence_json,
                confidence      = excluded.confidence,
                mx_valid        = excluded.mx_valid,
                is_role_address = excluded.is_role_address,
                is_disposable   = excluded.is_disposable,
                retrieved_at    = datetime('now'),
                raw_json        = COALESCE(excluded.raw_json, contact_emails.raw_json)
            """,
            (
                candidate_id, email, status, source,
                json.dumps(evidence or {}, ensure_ascii=False, default=str),
                confidence,
                None if mx_valid is None else int(mx_valid),
                int(is_role_address), int(is_disposable),
                json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
            ),
        )
        row = self.conn.execute(
            "SELECT id FROM contact_emails WHERE candidate_id = ? AND email = ?",
            (candidate_id, email),
        ).fetchone()
        return int(row["id"])

    def get(self, candidate_id: int, email: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM contact_emails WHERE candidate_id = ? AND email = ?",
            (candidate_id, (email or "").strip().lower()),
        ).fetchone()
        return _row_to_contact(r) if r else None

    def for_candidate(self, candidate_id: int) -> list[dict[str, Any]]:
        """Best evidence first — the order the UI should render them in."""
        rows = [
            _row_to_contact(r)
            for r in self.conn.execute(
                "SELECT * FROM contact_emails WHERE candidate_id = ?", (candidate_id,)
            )
        ]
        rows.sort(key=lambda d: (-STATUS_RANK.get(str(d["status"]), 0), str(d["email"])))
        return rows

    def best(self, candidate_id: int) -> dict[str, Any] | None:
        rows = self.for_candidate(candidate_id)
        return rows[0] if rows else None

    def for_candidates(self, candidate_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
        """One query for a whole table page, rather than N."""
        ids = [int(i) for i in candidate_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        out: dict[int, list[dict[str, Any]]] = {}
        for r in self.conn.execute(
            f"SELECT * FROM contact_emails WHERE candidate_id IN ({placeholders})", ids
        ):
            out.setdefault(int(r["candidate_id"]), []).append(_row_to_contact(r))
        for rows in out.values():
            rows.sort(key=lambda d: (-STATUS_RANK.get(str(d["status"]), 0), str(d["email"])))
        return out

    def delete_for_candidate(self, candidate_id: int) -> int:
        """The erasure primitive — §6.1. Blocklisting calls this."""
        cur = self.conn.execute(
            "DELETE FROM contact_emails WHERE candidate_id = ?", (candidate_id,)
        )
        return cur.rowcount or 0

    def delete(self, candidate_id: int, email: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM contact_emails WHERE candidate_id = ? AND email = ?",
            (candidate_id, (email or "").strip().lower()),
        )
        return cur.rowcount or 0

    def counts_by_status(self) -> dict[str, int]:
        return {
            str(r["status"]): int(r["n"])
            for r in self.conn.execute(
                "SELECT status, COUNT(*) n FROM contact_emails GROUP BY status"
            )
        }

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM contact_emails").fetchone()[0])


class EnrichmentLogRepo:
    """The audit trail: what each step did, and what it cost."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        step: str,
        *,
        candidate_id: int | None = None,
        company_id: int | None = None,
        run_id: int | None = None,
        provider: str | None = None,
        credits_spent: float = 0.0,
        outcome: str | None = None,
        detail: str | None = None,
    ) -> int:
        """Append one line. `detail` must never carry an address (see the DDL)."""
        cur = self.conn.execute(
            """
            INSERT INTO enrichment_log
                   (candidate_id, company_id, run_id, step, provider,
                    credits_spent, outcome, detail)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (candidate_id, company_id, run_id, step, provider,
             float(credits_spent), outcome, detail),
        )
        return int(cur.lastrowid or 0)

    def for_candidate(self, candidate_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM enrichment_log WHERE candidate_id = ? ORDER BY id ASC",
                (candidate_id,),
            )
        ]

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM enrichment_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def credits_spent(self, provider: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(credits_spent), 0) AS c FROM enrichment_log"
        args: tuple[Any, ...] = ()
        if provider:
            sql += " WHERE provider = ?"
            args = (provider,)
        return float(self.conn.execute(sql, args).fetchone()["c"])


#: Evidence ordering for `contact_emails.status` — §4. `upsert` refuses to move
#: a row down this ladder, so a cheap guess can never bury a proved address.
STATUS_RANK: dict[str, int] = {
    "not_found": 0,
    "guess": 1,
    "probable": 2,
    "verified": 3,
}


def _row_to_contact(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    try:
        d["evidence"] = json.loads(d.get("evidence_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["evidence"] = {}
    d["mx_valid"] = None if d.get("mx_valid") is None else bool(d["mx_valid"])
    d["is_role_address"] = bool(d.get("is_role_address"))
    d["is_disposable"] = bool(d.get("is_disposable"))
    return d


def _row_to_company(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["aliases"] = loads(d.get("aliases"))
    d["negative_keywords"] = loads(d.get("negative_keywords"))
    d["active"] = bool(d.get("active", 1))
    return d


def _row_to_role(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["aliases"] = loads(d.get("aliases"))
    d["seniority"] = loads(d.get("seniority"))
    d["keywords"] = loads(d.get("keywords"))
    d["active"] = bool(d.get("active", 1))
    return d


class SourceQueryRepo:
    """`source_queries`: a keyword source's cursors and yields (schema v7)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def for_source(self, source: str) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM source_queries WHERE source = ?", (source,)
        ).fetchall()
        return {(r["query"], r["country"]): dict(r) for r in rows}

    def record(self, source: str, query: str, country: str | None, *, requests: int,
               found: int, complete: bool, total: int | None = None) -> None:
        """One visit. `complete` = every page this visit needed was read, which
        is what moves the cursor; an interrupted visit is simply redone."""
        self.conn.execute(
            """
            INSERT INTO source_queries (source, query, country, runs, requests, found,
                                        last_total, last_fetched_at, backfilled)
            VALUES (?, ?, ?, 1, ?, ?, ?, CASE WHEN ? THEN datetime('now') END, ?)
            ON CONFLICT(source, query, country) DO UPDATE SET
                runs = runs + 1,
                requests = requests + excluded.requests,
                found = found + excluded.found,
                last_total = COALESCE(excluded.last_total, last_total),
                last_fetched_at = COALESCE(excluded.last_fetched_at, last_fetched_at),
                backfilled = MAX(backfilled, excluded.backfilled)
            """,
            (source, query, country or "", int(requests), int(found), total,
             int(complete), int(complete)),
        )


class AppStateRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, str(value)),
        )


class Repos:
    """Bundle of every repository over one connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.companies = CompanyRepo(conn)
        self.roles = RoleRepo(conn)
        self.queries = QueryRepo(conn)
        self.results = ResultRepo(conn)
        self.candidates = CandidateRepo(conn)
        self.scores = ScoreRepo(conn)
        self.crm = CRMRepo(conn)
        self.runs = RunRepo(conn)
        self.quota = QuotaRepo(conn)
        self.jobs = JobRepo(conn)
        self.job_sources = JobSourceRepo(conn)
        self.contacts = ContactRepo(conn)
        self.domains = CompanyDomainRepo(conn)
        self.enrichment = EnrichmentLogRepo(conn)
        self.source_queries = SourceQueryRepo(conn)
        self.state = AppStateRepo(conn)

    @staticmethod
    def writing() -> Any:
        """`with repos.writing(): …` around a group of writes (§1.1)."""
        return writing()


def open_repos(settings: Settings | None = None, *, path: Path | str | None = None) -> Repos:
    conn = get_conn(settings, path=path)
    init_schema(conn)
    return Repos(conn)


# ───────────────────────── resetting the people side ─────────────────────

#: What "Reset database" on PitWall's Setup page clears, children first: the
#: people found, everything said about them, and the search queue that found
#: them. The CRM notes go too (that is what a reset is), but only after a backup.
NETWORKING_RESET_TABLES = (
    "candidate_sources", "scores", "crm", "contact_emails", "candidates",
    "search_results", "queries",
)
#: Left alone on purpose: the job search, applications and everything else
#: RocketApply owns; `blocklist` (the GDPR erasure path — clearing it would let
#: an erased person be found again); `companies`/`roles` (the inputs, and
#: deleting a company cascades into its job sources); the paid email-domain
#: corpus and `enrichment_log`/`quota`, which the credit budgets are counted from.


class ResetRefused(RuntimeError):
    """The reset did not start: nothing was deleted."""


def reset_networking(conn: sqlite3.Connection, db_file: Path,
                     backups: Path | None = None) -> tuple[Path, dict[str, int]]:
    """Back up the whole file, then clear the networking tables only.

    The backup is SQLite's online backup (consistent even with a WAL open),
    checked before anything is deleted; if it cannot be made or does not read
    back, nothing is touched. Refuses while a networking run is going, since
    that run is writing the very rows this deletes.
    Returns the backup's path and the rows deleted per table.
    """
    if RunRepo(conn).active(commands=NETWORKING_COMMANDS):
        raise ResetRefused("a PitWall run is going on right now — stop it first")
    folder = backups if backups is not None else Path(db_file).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{Path(db_file).stem}-before-reset-{datetime.now():%Y-%m-%d-%H%M%S}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
        ok = dest.execute("PRAGMA quick_check").fetchone()[0]
        copied = dest.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    finally:
        dest.close()
    live = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    if ok != "ok" or copied != live:
        raise ResetRefused(f"the backup at {target} did not check out ({ok}); nothing deleted")

    deleted: dict[str, int] = {}
    kinds = ",".join("?" * len(NETWORKING_COMMANDS))
    with transaction(conn):
        for table in NETWORKING_RESET_TABLES:
            deleted[table] = conn.execute(f"DELETE FROM {table}").rowcount or 0
        for table in ("run_events", "run_progress"):
            deleted[table] = conn.execute(
                f"DELETE FROM {table} WHERE run_id IN"
                f" (SELECT id FROM runs WHERE command IN ({kinds}))", NETWORKING_COMMANDS,
            ).rowcount or 0
        deleted["runs"] = conn.execute(
            f"DELETE FROM runs WHERE command IN ({kinds})", NETWORKING_COMMANDS,
        ).rowcount or 0
    return target, deleted
