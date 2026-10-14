"""Reading a posting out of PitWall's database.

The job search already stores everything this engine needs — the full posting
text, the employer, the deterministic match with its matched and missing skills
— so nothing here re-polls or re-scores. It reads.

`job_matches.missing_skills` matters more than it looks: it is a second,
independent opinion on what the posting wants and the profile lacks, computed by
the existing seven-component scorer. The truth layer folds it in alongside the
analyzer's own answer, so a gap either one notices becomes a forbidden claim.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from ..db import get_conn, init_schema

__all__ = ["load_job", "shortlist", "existing_application", "JOB_COLUMNS"]

JOB_COLUMNS = (
    "j.id, j.title, j.description, j.location, j.url, j.posted_at, j.status, "
    "j.employment_type, j.company_id, j.source_id"
)


def _connection(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    """A connection with the schema guaranteed.

    `get_conn` opens the file; it does not create the tables. On a fresh install
    — someone opening the Applications Studio before ever running a search —
    that is the difference between an empty list and `no such table: jobs`.
    """
    if conn is not None:
        return conn
    conn = get_conn()
    init_schema(conn)
    return conn


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _optional_columns(conn: sqlite3.Connection) -> list[str]:
    """`posting_company` and friends arrive in the v5 -> v6 migration, so a
    database that predates it must still be readable."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    return [c for c in ("posting_company", "company_norm", "cluster_id", "seniority",
                        "remote_mode", "languages_required") if c in have]


def load_job(job_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """One posting, with its deterministic match folded in.

    Returns None rather than raising — "that job id is not here" is an ordinary
    answer for a CLI to give.
    """
    conn = _connection(conn)
    extra = _optional_columns(conn)
    select = JOB_COLUMNS + ("".join(f", j.{c}" for c in extra))
    job = _row(
        conn.execute(
            f"SELECT {select}, c.name AS company_name FROM jobs j "
            f"LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?",
            (job_id,),
        ).fetchone()
    )
    if job is None:
        return None
    # A job added by hand (`intake.add_posting`) says which language its letter
    # is written in; one the search found leaves it to the planner.
    forced = conn.execute(
        "SELECT json_extract(raw_json, '$.letter_language') FROM jobs WHERE id = ?"
        " AND json_valid(raw_json)", (job_id,)).fetchone()
    if forced and forced[0]:
        job["letter_language"] = str(forced[0])

    match = _row(
        conn.execute("SELECT * FROM job_matches WHERE job_id = ?", (job_id,)).fetchone()
    )
    if match:
        for key in ("matched_skills", "missing_skills", "missing_languages", "reasons_plus",
                    "reasons_minus", "filter_reasons", "components", "ai_reasons"):
            if isinstance(match.get(key), str):
                try:
                    match[key] = json.loads(match[key])
                except json.JSONDecodeError:
                    match[key] = []
        job["match"] = match
    return job


def shortlist(
    conn: sqlite3.Connection | None = None, *, limit: int = 20, min_score: float = 0.0
) -> list[dict[str, Any]]:
    """The best-scoring open postings that passed the hard filters."""
    conn = _connection(conn)
    # `posting_company` is what the advert itself says, and it is populated far
    # more often than `company_id`, which only links when the employer is also
    # on the people-search list.
    has_posting_company = "posting_company" in _optional_columns(conn)
    employer = (
        "COALESCE(NULLIF(j.posting_company, ''), c.name, '')"
        if has_posting_company else "COALESCE(c.name, '')"
    )
    rows = conn.execute(
        "SELECT j.id, j.title, j.location, j.url, m.score, m.label, "
        f"       {employer} AS company_name "
        "FROM jobs j JOIN job_matches m ON m.job_id = j.id "
        "LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE j.status = 'open' AND m.passed = 1 AND COALESCE(m.score, 0) >= ? "
        "ORDER BY m.score DESC LIMIT ?",
        (min_score, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def companies_for(
    job_ids: Sequence[int], conn: sqlite3.Connection | None = None
) -> dict[int, str]:
    """`{job_id: employer}` for these postings, in one query.

    The batch only learns a company name AFTER a letter is generated, which is
    too late for a research pre-pass that has to run once per employer rather
    than once per job. Same employer expression as `shortlist`, including the
    `_optional_columns` guard, because `posting_company` arrives in the v5 -> v6
    migration and a database that predates it must still be readable.
    """
    ids = [int(j) for j in job_ids]
    if not ids:
        return {}
    conn = _connection(conn)
    employer = (
        "COALESCE(NULLIF(j.posting_company, ''), c.name, '')"
        if "posting_company" in _optional_columns(conn) else "COALESCE(c.name, '')"
    )
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT j.id, {employer} AS company_name FROM jobs j "
        f"LEFT JOIN companies c ON c.id = j.company_id WHERE j.id IN ({marks})",
        ids,
    ).fetchall()
    return {int(r["id"]): str(r["company_name"] or "") for r in rows}


def existing_application(
    company: str, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Applications already sent to this employer.

    Worth a warning before generating another: two applications to the same
    company should be a deliberate choice, not an accident.
    """
    from ..jobsearch.tracker import at_company

    conn = _connection(conn)
    # Normalised, so "Anthropic PBC" and "anthropic" are the same employer —
    # the tracker's rule, not a second one.
    return [
        {k: a.get(k) for k in ("id", "title", "status", "date_applied")}
        for a in at_company(conn, company)
        if a.get("status") != "SKIPPED"
    ]
