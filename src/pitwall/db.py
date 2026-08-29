"""SQLite connection, schema and repositories — §4.2 of PITWALL_PLAN.md.

Raw `sqlite3` with a thin repository layer; no ORM. The DDL below is the
plan's schema verbatim, plus a `quota` table for the free-tier guards of §13.2
and a `run_progress` table so the Streamlit worker thread can stream progress
without touching the UI thread (§10, "poll a progress row in SQLite").
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

SCHEMA_VERSION = 3

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

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
"""

_local = threading.local()


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
    """Explicit transaction (connections run in autocommit / isolation_level=None)."""
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
    * v2 -> v3 added `scores.provider` (§0.4) — the CREATE TABLE above only
      applies to a fresh database, so an existing 2.0 MB file needs the ALTER.
    """
    _add_column(conn, "search_results", "processed", "INTEGER DEFAULT 0")
    _add_column(conn, "scores", "provider", "TEXT")


def vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")


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
        """evidence_count = number of DISTINCT queries that surfaced this person."""
        n = self.conn.execute(
            """
            SELECT COUNT(DISTINCT sr.query_id) AS n
              FROM candidate_sources cs
              JOIN search_results sr ON sr.id = cs.search_result_id
             WHERE cs.candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()["n"]
        self.conn.execute("UPDATE candidates SET evidence_count = ? WHERE id = ?", (n, candidate_id))
        return int(n)

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

    def awaiting_llm(self, prompt_version: str, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT s.*, c.name AS company_name, r.title AS role_title
              FROM scores s
              LEFT JOIN companies c ON c.id = s.company_id
              LEFT JOIN roles r     ON r.id = s.role_id
             WHERE s.prompt_version = ?
               AND s.llm_score IS NULL
               AND s.verdict IN ('pending_llm','needs_review')
             ORDER BY s.heuristic_score DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, (prompt_version,))]

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


class RunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(self, params: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (params, status) VALUES (?, 'running')",
            (json.dumps(params, ensure_ascii=False, default=str),),
        )
        run_id = int(cur.lastrowid or 0)
        self.conn.execute(
            "INSERT INTO run_progress (run_id, stage, message) VALUES (?, 'start', 'queued')"
            " ON CONFLICT DO NOTHING",
            (run_id,),
        )
        return run_id

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

    def latest(self, n: int = 10) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,))
        ]

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

    def set_credits(self, provider: str, credits: int) -> None:
        self.conn.execute(
            """
            INSERT INTO quota (provider, day, requests, credits) VALUES (?,?,0,?)
            ON CONFLICT(provider, day) DO UPDATE SET credits = excluded.credits
            """,
            (provider, today_utc(), max(0, credits)),
        )

    def spend_credits(self, provider: str, n: int = 1) -> int | None:
        cur = self.credits(provider)
        if cur is None:
            return None
        new = max(0, cur - n)
        self.set_credits(provider, new)
        return new

    def history(self, days: int = 30) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM quota ORDER BY day DESC, provider LIMIT ?", (days * 6,)
            )
        ]


# ─────────────────────────── row adapters ───────────────────────────


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


def open_repos(settings: Settings | None = None, *, path: Path | str | None = None) -> Repos:
    conn = get_conn(settings, path=path)
    init_schema(conn)
    return Repos(conn)
