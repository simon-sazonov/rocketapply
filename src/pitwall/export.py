"""The joined leads view and CSV/XLSX/Markdown export — Phase 8.

`LEADS_SQL` is the single SQL join every read goes through (§10, "All reads go
through a `@st.cache_data(ttl=30)` function that returns a pandas DataFrame
from a single SQL join"). Keeping it here means the Leads page, the Pipeline
page, Analytics and the exporter all see exactly the same rows.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Settings, get_settings
from .db import loads

log = logging.getLogger(__name__)

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
),
-- One address row per candidate: the best-evidenced one. Ranked by the §4
-- status ladder, not by recency — a pattern guess found this morning must not
-- outrank an address a corroboration search proved last week.
best_contact AS (
    SELECT e.candidate_id, e.email, e.status, e.source, e.evidence_json,
           e.mx_valid, e.is_role_address,
           ROW_NUMBER() OVER (
               PARTITION BY e.candidate_id
               ORDER BY CASE e.status
                            WHEN 'verified'  THEN 3
                            WHEN 'probable'  THEN 2
                            WHEN 'guess'     THEN 1
                            ELSE 0
                        END DESC,
                        e.id DESC
           ) AS rn
      FROM contact_emails e
),
contact_totals AS (
    SELECT candidate_id, COUNT(*) AS n FROM contact_emails GROUP BY candidate_id
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
       m.followup_at       AS followup_at,
       ec.email            AS contact_email,
       ec.status           AS contact_status,
       ec.source           AS contact_source,
       ec.evidence_json    AS contact_evidence_json,
       ec.mx_valid         AS contact_mx_valid,
       ec.is_role_address  AS contact_is_role,
       COALESCE(ct.n, 0)   AS contact_count
  FROM candidates c
  LEFT JOIN best b       ON b.candidate_id = c.id AND b.rn = 1
  LEFT JOIN companies co ON co.id = b.company_id
  LEFT JOIN roles ro     ON ro.id = b.role_id
  LEFT JOIN crm m        ON m.candidate_id = c.id
  LEFT JOIN best_contact ec ON ec.candidate_id = c.id AND ec.rn = 1
  LEFT JOIN contact_totals ct ON ct.candidate_id = c.id
"""

EXPORT_COLUMNS = [
    "name", "title", "company", "location", "score", "verdict", "status",
    "target_company", "target_role", "role_family", "seniority",
    "is_current_employee", "company_match", "role_match", "evidence",
    "profile_url", "note", "tags", "followup_at", "contacted_at",
]

#: Deliberately NOT in `EXPORT_COLUMNS` — §6.1 of PITWALL_V3_EMAIL_ENRICHMENT.md.
#:
#: A CSV leaves the application. Once it is in Downloads, on a USB stick, or
#: attached to an email it is outside every control this database has: the
#: blocklist cannot erase it and the retention timer cannot expire it. So the
#: gate is opt-in and loud, not a default with a footnote. `include_contacts`
#: is the only way these columns are ever written.
CONTACT_COLUMNS = [
    "contact_email", "contact_status", "contact_source", "contact_evidence",
]

#: What `--include-contacts` makes the caller acknowledge, in one sentence.
CONTACT_EXPORT_WARNING = (
    "This file will contain email addresses. They leave the application with it: "
    "deleting a person here will not delete them from the export, and the "
    "180-day retention rule does not reach it. Most of them are unconfirmed "
    "inferences, not facts — check the contact_status column before writing to "
    "anyone."
)


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
    df["contact_evidence"] = df["contact_evidence_json"].map(_loads_dict)
    df = df.drop(
        columns=["reasons_json", "red_flags_json", "tags_json", "contact_evidence_json"]
    )
    df["contact_status"] = df["contact_status"].fillna("not_found")
    df["contact_count"] = pd.to_numeric(df["contact_count"], errors="coerce").fillna(0).astype(int)
    df["contact_mx_valid"] = df["contact_mx_valid"].map({1: True, 0: False}).astype("boolean")
    df["contact_is_role"] = df["contact_is_role"].fillna(0).astype(bool)

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


def _loads_dict(value: Any) -> dict[str, Any]:
    """`evidence_json` as a dict. A malformed or absent blob is an empty one —
    evidence that cannot be read is evidence that cannot be shown."""
    import json

    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        out = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _empty_frame() -> pd.DataFrame:
    cols = [
        "candidate_id", "name", "title", "company", "location", "headline",
        "profile_url", "slug", "evidence", "first_seen", "last_seen", "score",
        "heuristic_score", "llm_score", "verdict", "is_current_employee",
        "company_match", "role_match", "seniority", "confidence", "reasons",
        "red_flags", "model", "scored_at", "target_company", "series", "tier",
        "target_role", "role_family", "status", "note", "tags", "contacted_at",
        "followup_at", "contact_email", "contact_status", "contact_source",
        "contact_evidence", "contact_mx_valid", "contact_is_role", "contact_count",
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


def _flatten(df: pd.DataFrame, *, include_contacts: bool = False) -> pd.DataFrame:
    """The exported column set. Addresses only ever appear on request (§6.1)."""
    out = df.copy()
    for col in ("tags", "reasons", "red_flags"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: " | ".join(str(x) for x in (v or [])))
    cols = [c for c in EXPORT_COLUMNS if c in out.columns]
    extra = [c for c in ("reasons", "red_flags") if c in out.columns]
    contacts: list[str] = []
    if include_contacts:
        log.warning("contact export: %s", CONTACT_EXPORT_WARNING)
        if "contact_evidence" in out.columns:
            # The evidence travels WITH the address, always. An exported column
            # of bare addresses would strip exactly the context §4 exists to
            # keep attached: a `guess` in a spreadsheet reads as a fact.
            out["contact_evidence"] = out["contact_evidence"].map(evidence_text)
        contacts = [c for c in CONTACT_COLUMNS if c in out.columns]
    return out[cols + extra + contacts]


def evidence_text(evidence: Any) -> str:
    """`{"pattern": "first.last", "sample_size": 3}` -> `pattern first.last (n=3)`.

    §4: "the label must always carry its evidence". This is that sentence, and
    it is shared by the CSV writer, the Markdown writer and the Streamlit chip
    so all three say the same thing.
    """
    if not isinstance(evidence, dict) or not evidence:
        return ""
    bits: list[str] = []
    pattern = evidence.get("pattern")
    if pattern:
        n = evidence.get("sample_size")
        bits.append(f"pattern {pattern}" + (f" (n={int(n)})" if n else " (n=0)"))
    url = evidence.get("corroborating_url")
    if url:
        bits.append(f"published: {evidence.get('corroborating_domain') or url}")
    if evidence.get("github_repo"):
        bits.append(f"git commit in {evidence['github_repo']}")
    mx = evidence.get("mx_host")
    if mx:
        bits.append(f"mail host {mx}")
    if evidence.get("provider_status"):
        bits.append(f"hunter: {evidence['provider_status']}")
    return " · ".join(bits)


def export_path(name: str, suffix: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return settings.export_dir / f"{name}-{stamp}.{suffix}"


def to_csv(
    df: pd.DataFrame, path: Path | str | None = None, settings: Settings | None = None,
    *, include_contacts: bool = False,
) -> Path:
    target = Path(path) if path else export_path("pitwall-leads", "csv", settings)
    _flatten(df, include_contacts=include_contacts).to_csv(target, index=False, encoding="utf-8")
    return target


def to_xlsx(
    df: pd.DataFrame, path: Path | str | None = None, settings: Settings | None = None,
    *, include_contacts: bool = False,
) -> Path:
    target = Path(path) if path else export_path("pitwall-leads", "xlsx", settings)
    _flatten(df, include_contacts=include_contacts).to_excel(
        target, index=False, sheet_name="leads"
    )
    return target


def to_markdown(
    df: pd.DataFrame, path: Path | str | None = None, settings: Settings | None = None,
    *, include_contacts: bool = False,
) -> Path:
    """A contact sheet — nice for pasting into a notes app before an event."""
    target = Path(path) if path else export_path("pitwall-contacts", "md", settings)
    target.write_text(markdown_text(df, include_contacts=include_contacts), encoding="utf-8")
    return target


def markdown_text(df: pd.DataFrame, *, include_contacts: bool = False) -> str:
    """The contact sheet.

    Named "contact sheet" since v1 and it has never held an address; that stays
    true unless the caller asks. When it does ask, every address is rendered
    with its status word in front of it, because this is the format that gets
    pasted into a phone before an event and read at a glance.
    """
    if include_contacts:
        log.warning("contact export: %s", CONTACT_EXPORT_WARNING)
    lines = [
        "# PitWall contact sheet",
        f"_{len(df)} people · generated {datetime.now():%Y-%m-%d %H:%M}_",
        "",
    ]
    if include_contacts:
        lines += [f"> ⚠️ {CONTACT_EXPORT_WARNING}", ""]
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
            if include_contacts and _text(row.get("contact_email")):
                status = str(row.get("contact_status") or "guess")
                detail = evidence_text(row.get("contact_evidence"))
                lines.append(
                    f"  ✉️ **{status}** `{row['contact_email']}`"
                    + (f" — {detail}" if detail else "")
                    + "  "
                )
            note = str(row.get("note") or "").strip()
            if note:
                for nl in note.splitlines():
                    lines.append(f"  > {nl}")
            lines.append("")
    return "\n".join(lines)


def export_view(
    df: pd.DataFrame, fmt: str = "csv", path: Path | str | None = None,
    settings: Settings | None = None, *, include_contacts: bool = False,
) -> Path:
    match fmt.lower():
        case "csv":
            return to_csv(df, path, settings, include_contacts=include_contacts)
        case "xlsx" | "excel":
            return to_xlsx(df, path, settings, include_contacts=include_contacts)
        case "md" | "markdown":
            return to_markdown(df, path, settings, include_contacts=include_contacts)
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


def _text(value: Any) -> str | None:
    """A pandas cell as a string, or None. NULL comes back as float('nan')."""
    return value if isinstance(value, str) and value.strip() else None


def role_family_for(title: Any) -> str | None:
    """Guess a role family from a job title, using the heuristic layer's own
    keyword table — so a posting and a person are classified the same way.

    Takes `Any` deliberately: this reads a pandas column, and pandas represents
    a NULL title as `float('nan')`. 147 of the 384 people in the real database
    have no title, so a `str` annotation here would be a lie that raised on the
    first real row.
    """
    from .profiles import active
    from .scoring.heuristics import FAMILY_KEYWORDS

    if not isinstance(title, str):
        return None
    low = " " + title.lower() + " "
    best: tuple[int, str] | None = None
    for family, words in (active().family_keywords or FAMILY_KEYWORDS).items():
        for word in words:
            if word in low and (best is None or len(word) > best[0]):
                best = (len(word), family)
    return best[1] if best else None


PROXIMITY_LABELS = {
    2: "same role family",
    1: "adjacent family",
    0: "different discipline",
}


def role_proximity(job_title: Any, person_family: Any, person_title: Any) -> int:
    """How close is this person's work to this posting? 2 / 1 / 0.

    Deliberately coarse. The useful signal is "there is someone in that
    department", not a similarity score — the person is not applying for the
    job, they are the reason you have something to say about it.

    The person's own title wins over `person_family` when there is one, and that
    ordering matters: the stored family is the family of the role whose *query*
    surfaced them, which says what we were looking for, not what they do. A
    mechanic found by a "Data Scientist" search is not in the data department.
    """
    job_family = role_family_for(job_title)
    if not job_family:
        return 0
    if isinstance(person_title, str) and person_title.strip():
        title_family = role_family_for(person_title)
        return 2 if title_family == job_family else (1 if title_family else 0)
    if not isinstance(person_family, str) or not person_family:
        return 0
    return 2 if person_family == job_family else 1


def openings_frame(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    company: str | None = None,
    min_score: float = 0.0,
    prompt_version: str | None = None,
) -> list[dict[str, Any]]:
    """**The point of Phase 2** — §2.5: each open posting, and the people you
    already have at that company, ranked, with their CRM status.

    The sentence this exists to produce is: "Alpine posted a Performance
    Engineer role 3 days ago — you already have 4 people in that department,
    none contacted."
    """
    settings = settings or get_settings()
    version = prompt_version or settings.prompt_version

    jobs = [
        dict(r)
        for r in conn.execute(
            """
            SELECT j.*, c.name AS company_name, s.name AS source_name
              FROM jobs j
              LEFT JOIN companies c   ON c.id = j.company_id
              LEFT JOIN job_sources s ON s.id = j.source_id
             WHERE j.status = 'open'
             ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC
            """
        )
    ]
    if company:
        jobs = [j for j in jobs if (j["company_name"] or "").casefold() == company.casefold()]
    if not jobs:
        return []

    leads = leads_frame(conn, settings, prompt_version=version)
    now = datetime.now(UTC)

    out: list[dict[str, Any]] = []
    for job in jobs:
        people: list[dict[str, Any]] = []
        if not leads.empty and job.get("company_id"):
            same = leads[leads["target_company"] == job["company_name"]]
            same = same[same["score"].fillna(0.0) >= min_score]
            if settings.hide_ignored_by_default:
                same = same[same["status"] != "ignored"]
            for _, row in same.iterrows():
                proximity = role_proximity(
                    job["title"], row.get("role_family"), row.get("title")
                )
                people.append({
                    "candidate_id": int(row["candidate_id"]),
                    "name": _text(row.get("name")) or "—",
                    # Normalised here rather than at each caller: pandas hands
                    # back float('nan') for a NULL, and "nan" rendered as a job
                    # title in the CLI before this.
                    "title": _text(row.get("title")),
                    "score": float(row.get("score") or 0.0),
                    "status": _text(row.get("status")) or "new",
                    "verdict": _text(row.get("verdict")) or "unscored",
                    "profile_url": _text(row.get("profile_url")) or "",
                    "proximity_rank": proximity,
                    "proximity": PROXIMITY_LABELS[proximity],
                })
            # Closest discipline first, then best-scored — that is the order you
            # would actually write to them in.
            people.sort(key=lambda p: (-p["proximity_rank"], -p["score"]))

        posted = job.get("posted_at") or job.get("first_seen_at")
        age_days = None
        if posted:
            try:
                stamp = datetime.fromisoformat(str(posted))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                age_days = max(0, (now - stamp).days)
            except ValueError:
                age_days = None

        out.append({
            **job,
            "age_days": age_days,
            "people": people,
            "people_count": len(people),
            "uncontacted_count": sum(1 for p in people if p["status"] in ("new", "saved")),
            "same_family_count": sum(1 for p in people if p["proximity_rank"] == 2),
            # Counted within the discipline, not across the company: the
            # headline sentence says "N people in that department, M of whom
            # you have not contacted", and M has to be a subset of N.
            "uncontacted_same_family": sum(
                1 for p in people
                if p["proximity_rank"] == 2 and p["status"] in ("new", "saved")
            ),
        })

    # Postings where you know someone come first — that is the whole ranking.
    out.sort(key=lambda j: (-j["same_family_count"], -j["people_count"], j["age_days"] or 999))
    return out


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
