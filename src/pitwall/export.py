"""The joined leads view and CSV/XLSX/Markdown export — Phase 8.

`LEADS_SQL` is the single SQL join every read goes through (§10, "All reads go
through a `@st.cache_data(ttl=30)` function that returns a pandas DataFrame
from a single SQL join"). Keeping it here means the Leads page, the Pipeline
page, Analytics and the exporter all see exactly the same rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Settings, get_settings
from .db import loads

#: One score row per candidate: the best-scoring company×role pairing.
LEADS_SQL = """
WITH best AS (
    SELECT s.*,
           ROW_NUMBER() OVER (
               PARTITION BY s.candidate_id
               ORDER BY COALESCE(s.final_score, s.heuristic_score, 0) DESC, s.id DESC
           ) AS rn
      FROM scores s
     WHERE s.prompt_version = :prompt_version
)
SELECT c.id                AS candidate_id,
       c.full_name         AS name,
       c.current_title     AS title,
       c.current_company   AS company,
       c.location          AS location,
       c.headline          AS headline,
       c.profile_url       AS profile_url,
       c.linkedin_slug     AS slug,
       c.evidence_count    AS evidence,
       c.first_seen_at     AS first_seen,
       c.last_seen_at      AS last_seen,
       b.final_score       AS score,
       b.heuristic_score   AS heuristic_score,
       b.llm_score         AS llm_score,
       b.verdict           AS verdict,
       b.is_current_employee AS is_current_employee,
       b.company_match     AS company_match,
       b.role_match        AS role_match,
       b.seniority         AS seniority,
       b.confidence        AS confidence,
       b.reasons           AS reasons_json,
       b.red_flags         AS red_flags_json,
       b.model             AS model,
       b.scored_at         AS scored_at,
       co.name             AS target_company,
       co.series           AS series,
       co.tier             AS tier,
       ro.title            AS target_role,
       ro.family           AS role_family,
       COALESCE(m.status, 'new') AS status,
       m.note              AS note,
       m.tags              AS tags_json,
       m.contacted_at      AS contacted_at,
       m.followup_at       AS followup_at
  FROM candidates c
  LEFT JOIN best b       ON b.candidate_id = c.id AND b.rn = 1
  LEFT JOIN companies co ON co.id = b.company_id
  LEFT JOIN roles ro     ON ro.id = b.role_id
  LEFT JOIN crm m        ON m.candidate_id = c.id
"""

EXPORT_COLUMNS = [
    "name", "title", "company", "location", "score", "verdict", "status",
    "target_company", "target_role", "role_family", "seniority",
    "is_current_employee", "company_match", "role_match", "evidence",
    "profile_url", "note", "tags", "followup_at", "contacted_at",
]


def leads_frame(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    prompt_version: str | None = None,
) -> pd.DataFrame:
    """The full joined leads table as a DataFrame."""
    settings = settings or get_settings()
    version = prompt_version or settings.prompt_version
    df = pd.read_sql_query(LEADS_SQL, conn, params={"prompt_version": version})
    if df.empty:
        return _empty_frame()

    df["reasons"] = df["reasons_json"].map(loads)
    df["red_flags"] = df["red_flags_json"].map(loads)
    df["tags"] = df["tags_json"].map(loads)
    df = df.drop(columns=["reasons_json", "red_flags_json", "tags_json"])

    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)
    df["heuristic_score"] = pd.to_numeric(df["heuristic_score"], errors="coerce")
    df["llm_score"] = pd.to_numeric(df["llm_score"], errors="coerce")
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    df["evidence"] = pd.to_numeric(df["evidence"], errors="coerce").fillna(0).astype(int)
    df["verdict"] = df["verdict"].fillna("unscored")
    df["status"] = df["status"].fillna("new")
    # A nullable boolean, so "unknown" stays distinct from False in the UI.
    df["is_current_employee"] = (
        df["is_current_employee"].map({1: True, 0: False}).astype("boolean")
    )
    return df.sort_values("score", ascending=False, ignore_index=True)


def _empty_frame() -> pd.DataFrame:
    cols = [
        "candidate_id", "name", "title", "company", "location", "headline",
        "profile_url", "slug", "evidence", "first_seen", "last_seen", "score",
        "heuristic_score", "llm_score", "verdict", "is_current_employee",
        "company_match", "role_match", "seniority", "confidence", "reasons",
        "red_flags", "model", "scored_at", "target_company", "series", "tier",
        "target_role", "role_family", "status", "note", "tags", "contacted_at",
        "followup_at",
    ]
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


def filter_frame(
    df: pd.DataFrame,
    *,
    companies: list[str] | None = None,
    families: list[str] | None = None,
    verdicts: list[str] | None = None,
    statuses: list[str] | None = None,
    seniorities: list[str] | None = None,
    score_range: tuple[float, float] = (0.0, 100.0),
    current_only: bool = False,
    hide_ignored: bool = True,
    text: str = "",
    tags: list[str] | None = None,
    new_since: str | None = None,
) -> pd.DataFrame:
    """Apply the Leads-page sidebar filters. Pure pandas, no SQL."""
    if df.empty:
        return df
    out = df
    if companies:
        out = out[out["target_company"].isin(companies) | out["company"].isin(companies)]
    if families:
        out = out[out["role_family"].isin(families)]
    if verdicts:
        out = out[out["verdict"].isin(verdicts)]
    if statuses:
        out = out[out["status"].isin(statuses)]
    if seniorities:
        out = out[out["seniority"].isin(seniorities)]
    lo, hi = score_range
    out = out[(out["score"] >= lo) & (out["score"] <= hi)]
    if current_only:
        out = out[out["is_current_employee"] == True]  # noqa: E712
    if hide_ignored:
        out = out[out["status"] != "ignored"]
    if text:
        needle = text.strip().lower()
        haystack = (
            out["name"].fillna("").str.lower()
            + " " + out["title"].fillna("").str.lower()
            + " " + out["company"].fillna("").str.lower()
            + " " + out["headline"].fillna("").str.lower()
        )
        out = out[haystack.str.contains(needle, regex=False, na=False)]
    if tags:
        wanted = {t.lower() for t in tags}
        out = out[out["tags"].map(lambda ts: bool(wanted & {str(t).lower() for t in (ts or [])}))]
    if new_since:
        out = out[out["first_seen"].fillna("") >= new_since]
    return out


# ─────────────────────────────── writers ────────────────────────────────


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ("tags", "reasons", "red_flags"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: " | ".join(str(x) for x in (v or [])))
    cols = [c for c in EXPORT_COLUMNS if c in out.columns]
    extra = [c for c in ("reasons", "red_flags") if c in out.columns]
    return out[cols + extra]


def export_path(name: str, suffix: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return settings.export_dir / f"{name}-{stamp}.{suffix}"


def to_csv(df: pd.DataFrame, path: Path | str | None = None, settings: Settings | None = None) -> Path:
    target = Path(path) if path else export_path("pitwall-leads", "csv", settings)
    _flatten(df).to_csv(target, index=False, encoding="utf-8")
    return target


def to_xlsx(df: pd.DataFrame, path: Path | str | None = None, settings: Settings | None = None) -> Path:
    target = Path(path) if path else export_path("pitwall-leads", "xlsx", settings)
    _flatten(df).to_excel(target, index=False, sheet_name="leads")
    return target


def to_markdown(df: pd.DataFrame, path: Path | str | None = None, settings: Settings | None = None) -> Path:
    """A contact sheet — nice for pasting into a notes app before an event."""
    target = Path(path) if path else export_path("pitwall-contacts", "md", settings)
    target.write_text(markdown_text(df), encoding="utf-8")
    return target


def markdown_text(df: pd.DataFrame) -> str:
    lines = [
        "# PitWall contact sheet",
        f"_{len(df)} people · generated {datetime.now():%Y-%m-%d %H:%M}_",
        "",
    ]
    for company, group in df.groupby(df["target_company"].fillna("Unassigned"), sort=True):
        lines.append(f"## {company}")
        lines.append("")
        for _, row in group.sort_values("score", ascending=False).iterrows():
            score = f"{row['score']:.0f}" if pd.notna(row["score"]) else "–"
            bits = [f"**{row['name'] or 'Unknown'}** — {row['title'] or 'role unknown'}"]
            if row.get("location"):
                bits.append(str(row["location"]))
            lines.append(f"- {' · '.join(bits)}  ")
            lines.append(f"  score {score} · {row.get('verdict', '')} · {row.get('status', '')}  ")
            if row.get("profile_url"):
                lines.append(f"  <{row['profile_url']}>  ")
            note = str(row.get("note") or "").strip()
            if note:
                for nl in note.splitlines():
                    lines.append(f"  > {nl}")
            lines.append("")
    return "\n".join(lines)


def export_view(
    df: pd.DataFrame, fmt: str = "csv", path: Path | str | None = None, settings: Settings | None = None
) -> Path:
    match fmt.lower():
        case "csv":
            return to_csv(df, path, settings)
        case "xlsx" | "excel":
            return to_xlsx(df, path, settings)
        case "md" | "markdown":
            return to_markdown(df, path, settings)
        case _:
            raise ValueError(f"unknown export format {fmt!r}")


# ───────────────────────────── analytics ────────────────────────────────


def coverage_matrix(df: pd.DataFrame, *, strong_at: float = 75.0) -> pd.DataFrame:
    """company × role_family grid of strong matches — the Analytics heatmap.

    Empty cells are your to-do list: they almost always mean a missing alias,
    not an absent team (§10).
    """
    if df.empty:
        return pd.DataFrame(columns=["target_company", "role_family", "strong", "total"])
    work = df.dropna(subset=["target_company", "role_family"]).copy()
    if work.empty:
        return pd.DataFrame(columns=["target_company", "role_family", "strong", "total"])
    work["is_strong"] = work["score"] >= strong_at
    grouped = (
        work.groupby(["target_company", "role_family"], dropna=False)
        .agg(strong=("is_strong", "sum"), total=("candidate_id", "count"))
        .reset_index()
    )
    grouped["strong"] = grouped["strong"].astype(int)
    return grouped


def funnel_counts(conn: sqlite3.Connection, settings: Settings | None = None) -> dict[str, int]:
    """queries -> SERP rows -> candidates -> passed heuristics -> strong."""
    settings = settings or get_settings()
    q = lambda sql, *a: int(conn.execute(sql, a).fetchone()[0])  # noqa: E731
    return {
        "queries": q("SELECT COUNT(*) FROM queries WHERE status = 'done'"),
        "serp_rows": q("SELECT COUNT(*) FROM search_results"),
        "candidates": q("SELECT COUNT(*) FROM candidates"),
        "passed_heuristics": q(
            "SELECT COUNT(DISTINCT candidate_id) FROM scores WHERE heuristic_score >= ?",
            settings.heuristic_threshold,
        ),
        "strong": q(
            "SELECT COUNT(DISTINCT candidate_id) FROM scores WHERE final_score >= ?",
            settings.band_strong,
        ),
    }


def sweep_report(conn: sqlite3.Connection, settings: Settings | None = None) -> dict[str, Any]:
    """What a sweep actually achieved — §1.5's "record the result".

    Coverage before precision: v1 had searched 7 of 49 companies, so the next
    volume of leads was in the 42 that had never been looked at, not in a better
    threshold. `extraction` is the other half of the story — operator queries
    return cleaner titles, so those gaps should close as coverage grows.
    """
    settings = settings or get_settings()
    q = lambda sql, *a: int(conn.execute(sql, a).fetchone()[0])  # noqa: E731

    companies = q("SELECT COUNT(*) FROM companies WHERE active = 1")
    searched = q(
        """
        SELECT COUNT(DISTINCT q.company_id) FROM queries q
         WHERE q.status = 'done' AND q.company_id IS NOT NULL
        """
    )
    with_rows = q(
        """
        SELECT COUNT(DISTINCT q.company_id) FROM queries q
          JOIN search_results sr ON sr.query_id = q.id
         WHERE q.company_id IS NOT NULL
        """
    )
    with_people = q(
        """
        SELECT COUNT(DISTINCT q.company_id)
          FROM candidate_sources cs
          JOIN search_results sr ON sr.id = cs.search_result_id
          JOIN queries q         ON q.id = sr.query_id
         WHERE q.company_id IS NOT NULL
        """
    )
    candidates = q("SELECT COUNT(*) FROM candidates")
    return {
        "companies_active": companies,
        "companies_searched": searched,
        "companies_with_serp_rows": with_rows,
        "companies_with_people": with_people,
        "templates_fired": [
            r["template_id"]
            for r in conn.execute(
                "SELECT DISTINCT template_id FROM queries WHERE status = 'done'"
                " ORDER BY template_id"
            )
        ],
        "queries_done": q("SELECT COUNT(*) FROM queries WHERE status = 'done'"),
        "queries_failed": q("SELECT COUNT(*) FROM queries WHERE status = 'failed'"),
        "serp_rows": q("SELECT COUNT(*) FROM search_results"),
        "candidates": candidates,
        # The extraction gaps of §1.5: these should shrink as operator templates
        # replace the soft ones, because a `site:` result has a cleaner title.
        "extraction": {
            "no_company": q("SELECT COUNT(*) FROM candidates WHERE current_company IS NULL OR current_company = ''"),
            "no_title": q("SELECT COUNT(*) FROM candidates WHERE current_title IS NULL OR current_title = ''"),
            "no_location": q("SELECT COUNT(*) FROM candidates WHERE location IS NULL OR location = ''"),
            "single_source": q("SELECT COUNT(*) FROM candidates WHERE evidence_count <= 1"),
        },
        "verdicts": {
            r["verdict"] or "unscored": r["n"]
            for r in conn.execute(
                "SELECT verdict, COUNT(*) n FROM scores GROUP BY verdict ORDER BY n DESC"
            )
        },
    }


def zero_result_companies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Companies whose queries all came back empty — almost always a bad alias."""
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT co.name, co.tier, COUNT(q.id) AS queries,
                   COALESCE(SUM(q.results_count), 0) AS rows
              FROM companies co
              LEFT JOIN queries q ON q.company_id = co.id AND q.status = 'done'
             WHERE co.active = 1
             GROUP BY co.id
            HAVING queries > 0 AND rows = 0
             ORDER BY co.tier ASC, co.name ASC
            """
        )
    ]
