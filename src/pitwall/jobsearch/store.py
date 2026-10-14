"""SQL for the job search: normalised fields, match results, and the inbox.

The inbox is read in clusters, not rows (§11): one vacancy, however many
sources carried it, shown once, represented by its most direct source (§63),
with every source's link still available.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import JOB_SEARCH_COLUMNS, loads
from .dedupe import pick_canonical, source_rank
from .normalize import NORMALIZER_VERSION

_WRITABLE = {name for name, _ in JOB_SEARCH_COLUMNS} | {
    "salary_min", "salary_max", "salary_currency", "salary_text", "remote_regions",
    "skills_nice", "agency",
}

#: What the owner has done with a vacancy, for the inbox tabs.
VIEWS = ("new", "matches", "shortlist", "filtered", "dismissed", "all")

#: §67 — why a job was dismissed. Feeds later ranking (§68), so a fixed list.
DISMISS_REASONS = (
    "Too senior", "Wrong country", "No sponsorship", "Wrong role", "Salary",
    "Not interested", "Already applied", "Poor match", "Duplicate", "Closed",
)


# ─────────────────────────────── writing ─────────────────────────────────


def rows_to_normalize(conn: sqlite3.Connection, *, force: bool = False) -> list[dict[str, Any]]:
    """Open postings whose derived fields are missing, stale, or older than the
    current rules. `force` re-reads every open posting."""
    sql = """
        SELECT j.*, c.name AS networking_name
          FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
         WHERE j.status = 'open'
    """
    if not force:
        sql += (" AND (j.normalizer_version IS NULL OR j.normalizer_version != ?"
                " OR j.normalized_hash IS NOT j.content_hash)")
        return [dict(r) for r in conn.execute(sql, (NORMALIZER_VERSION,))]
    return [dict(r) for r in conn.execute(sql)]


def save_normalized(conn: sqlite3.Connection, job_id: int, fields: dict[str, Any]) -> None:
    cols = [k for k in fields if k in _WRITABLE]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                 [fields[c] for c in cols] + [job_id])


def rows_to_score(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT j.*, s.kind AS source_kind
          FROM jobs j LEFT JOIN job_sources s ON s.id = j.source_id
         WHERE j.status = 'open' AND j.normalizer_version IS NOT NULL
        """
    )]


def save_match(conn: sqlite3.Connection, job_id: int, *, config_hash: str, passed: bool,
               reasons: list[dict[str, str]], match: Any,
               weights: dict[str, float] | None = None) -> None:
    weights = weights or {}
    components = {name: comp.as_dict(weights.get(name, 0.0))
                  for name, comp in match.components.items()}
    conn.execute(
        """
        INSERT INTO job_matches (job_id, config_hash, passed, filter_reasons, score, label,
            components, reasons_plus, reasons_minus, matched_skills, missing_skills,
            missing_languages, visa_fit, scored_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(job_id) DO UPDATE SET
            config_hash=excluded.config_hash, passed=excluded.passed,
            filter_reasons=excluded.filter_reasons, score=excluded.score,
            label=excluded.label, components=excluded.components,
            reasons_plus=excluded.reasons_plus, reasons_minus=excluded.reasons_minus,
            matched_skills=excluded.matched_skills, missing_skills=excluded.missing_skills,
            missing_languages=excluded.missing_languages,
            visa_fit=excluded.visa_fit, scored_at=excluded.scored_at
        """,
        (job_id, config_hash, int(passed), json.dumps(reasons, ensure_ascii=False),
         match.score, match.label, json.dumps(components),
         json.dumps(match.plus, ensure_ascii=False), json.dumps(match.minus, ensure_ascii=False),
         json.dumps(match.matched_skills), json.dumps(match.missing_skills),
         json.dumps(getattr(match, "missing_languages", []), ensure_ascii=False),
         match.visa_fit),
    )


def save_ai_fit(conn: sqlite3.Connection, job_id: int, score: float | None,
                reasons: list[str], model: str | None) -> None:
    conn.execute(
        "UPDATE job_matches SET ai_score = ?, ai_reasons = ?, ai_model = ? WHERE job_id = ?",
        (score, json.dumps(reasons, ensure_ascii=False), model, job_id),
    )


# ─────────────────────────────── reviewing ───────────────────────────────


def _review(conn: sqlite3.Connection, cluster_id: int, **fields: Any) -> None:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
    conn.execute(
        f"""
        INSERT INTO job_review (cluster_id, {cols}, updated_at) VALUES (?, {marks}, datetime('now'))
        ON CONFLICT(cluster_id) DO UPDATE SET {updates}, updated_at = datetime('now')
        """,
        (cluster_id, *fields.values()),
    )


def mark_seen(conn: sqlite3.Connection, cluster_ids: list[int]) -> int:
    for cid in cluster_ids:
        _review(conn, int(cid), seen_at=_now(conn))
    return len(cluster_ids)


def dismiss(conn: sqlite3.Connection, cluster_id: int, reason: str | None) -> None:
    """Done with this vacancy, everywhere. The review row is keyed on the cluster,
    so Search stops showing it whichever source posts it again, and an application
    that has not gone out yet leaves Selected, Review and In process. One that
    went out stays: it is history, not a queue."""
    from . import tracker

    _review(conn, int(cluster_id), dismissed_at=_now(conn), dismissed_reason=reason,
            seen_at=_now(conn))
    tracker.skip_cluster(conn, int(cluster_id),
                         note=f"not interested: {reason}" if reason else "not interested")


def restore(conn: sqlite3.Connection, cluster_id: int) -> None:
    _review(conn, int(cluster_id), dismissed_at=None, dismissed_reason=None)


def set_note(conn: sqlite3.Connection, cluster_id: int, note: str | None) -> None:
    _review(conn, int(cluster_id), note=(note or "").strip() or None)


#: Why a job is a bad match. Kept apart from DISMISS_REASONS: a rating says
#: the ranking was wrong, a dismissal only that you are done with the job.
FEEDBACK_REASONS = (
    "Too senior", "Wrong kind of role", "Skills don't match", "Wrong country or city",
    "Needs sponsorship they won't give", "Language requirement", "Company or domain",
    "Salary", "Agency or unclear employer", "Other",
)


def set_feedback(conn: sqlite3.Connection, cluster_id: int, feedback: str | None) -> None:
    """§91: good_match / bad_match, for measuring whether ranking improves.
    Changing or clearing the rating clears the reason given for the old one."""
    _review(conn, int(cluster_id), feedback=feedback, feedback_reason=None,
            feedback_note=None)


def set_feedback_reason(conn: sqlite3.Connection, cluster_id: int, reason: str | None,
                        note: str | None = None) -> None:
    """Why a job was rated a bad match, in the owner's words."""
    _review(conn, int(cluster_id), feedback_reason=reason or None,
            feedback_note=(note or "").strip() or None)


def _now(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT datetime('now')").fetchone()[0])


# ─────────────────────────────── reading ─────────────────────────────────

_INBOX_SQL = """
    SELECT j.id, j.cluster_id, j.title, j.posting_company, j.company_id, j.target_company_id,
           j.company_norm, j.url, j.canonical_url, j.location, j.country, j.countries,
           j.city, j.cities, j.remote_status, j.remote_region, j.remote_regions,
           j.employment_types,
           j.experience_min, j.experience_max, j.experience_level, j.experience_required,
           j.experience_evidence, j.education_required, j.education_preferred,
           j.salary_min, j.salary_max, j.salary_currency, j.salary_text,
           j.visa_status, j.visa_evidence, j.visa_scope, j.relocation, j.role_labels,
           j.skills, j.skills_nice, j.agency,
           j.description, j.description_truncated, j.posted_at,
           j.first_seen_at, j.last_seen_at, j.status, j.content_changed_at,
           j.previous_description, j.possible_duplicate_of, j.language,
           j.languages_required,
           s.name AS source_name, s.kind AS source_kind,
           c.name AS networking_company,
           t.name AS target_name, t.priority AS target_priority, t.tags AS target_tags,
           m.passed, m.filter_reasons, m.score, m.label, m.components,
           m.reasons_plus, m.reasons_minus, m.matched_skills, m.missing_skills,
           m.visa_fit, m.ai_score, m.ai_reasons, m.config_hash
      FROM jobs j
      JOIN job_matches m          ON m.job_id = j.id
      LEFT JOIN job_sources s     ON s.id = j.source_id
      LEFT JOIN companies c       ON c.id = j.company_id
      LEFT JOIN target_companies t ON t.id = j.target_company_id
     WHERE j.cluster_id IS NOT NULL
"""


@dataclass
class Inbox:
    clusters: list[dict[str, Any]]
    since: str | None

    def view(self, name: str) -> list[dict[str, Any]]:
        out = [c for c in self.clusters if _in_view(c, name)]
        if name == "filtered":
            return sorted(out, key=lambda c: -(c.get("score") or 0))
        return sorted(out, key=lambda c: (-(c.get("score") or 0), c.get("title") or ""))

    def counts(self) -> dict[str, int]:
        counts = {name: len(self.view(name)) for name in VIEWS}
        counts["new_since_last_run"] = sum(
            1 for c in self.view("matches") if self.since and c["first_seen"] >= self.since
        )
        return counts


def _in_view(c: dict[str, Any], name: str) -> bool:
    is_open = c["status"] == "open"
    app = c.get("application")
    dismissed = bool(c.get("dismissed_at"))
    if name == "all":
        return True
    if name == "dismissed":
        return dismissed
    if name == "shortlist":
        return app is not None and app["status"] in ("SHORTLISTED", "CV_READY")
    if name == "filtered":
        return is_open and not c["passed"] and not dismissed
    if name == "matches":
        return is_open and bool(c["passed"]) and not dismissed
    if name == "new":
        unseen = not c.get("seen_at") or bool(c.get("changed_since_seen"))
        return is_open and bool(c["passed"]) and not dismissed and not app and unseen
    raise ValueError(f"unknown view {name!r}")


def previous_run_started(conn: sqlite3.Connection) -> str | None:
    """When the latest finished job-search run began — "new since last run" (§41).

    A vacancy first seen after that moment is one the latest run discovered.
    Before any run has finished there is no "since", and nothing is counted.
    """
    row = conn.execute(
        "SELECT started_at FROM runs WHERE command = 'job-search' AND status = 'done'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return str(row["started_at"]) if row else None


def load_inbox(conn: sqlite3.Connection) -> Inbox:
    """Every scored vacancy, one entry per cluster."""
    rows = [dict(r) for r in conn.execute(_INBOX_SQL)]
    reviews = {int(r["cluster_id"]): dict(r) for r in conn.execute("SELECT * FROM job_review")}
    apps: dict[int, dict[str, Any]] = {}
    by_company: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM applications ORDER BY id"):
        app = dict(r)
        if app.get("cluster_id") is not None:
            apps[int(app["cluster_id"])] = app
        if app.get("company_norm"):
            by_company.setdefault(app["company_norm"], []).append(app)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["cluster_id"]), []).append(row)

    clusters = []
    for cluster_id, members in grouped.items():
        best = dict(pick_canonical(members))
        review = reviews.get(cluster_id, {})
        seen = review.get("seen_at")
        changed = max((m.get("content_changed_at") or "" for m in members), default="")
        best.update({
            "cluster_id": cluster_id,
            "company": best.get("target_name") or best.get("posting_company")
            or best.get("networking_company") or "",
            "status": "open" if any(m["status"] == "open" for m in members) else "closed",
            "first_seen": min(m["first_seen_at"] or "" for m in members),
            "members": sorted(
                ({"source": m.get("source_name") or "?", "kind": m.get("source_kind"),
                  "url": m.get("url"), "status": m.get("status")} for m in members),
                key=lambda m: source_rank(m["kind"]),
            ),
            # Which of the owner's lists this vacancy belongs to — any source
            # carrying it counts, not only the one chosen as the Apply link.
            "on_target_list": any(m.get("target_company_id") for m in members),
            "on_people_list": any(m.get("company_id") for m in members),
            "target_tags": sorted({t for m in members for t in loads(m.get("target_tags"))}),
            "seen_at": seen,
            "changed_since_seen": bool(seen and changed and changed > seen),
            "dismissed_at": review.get("dismissed_at"),
            "dismissed_reason": review.get("dismissed_reason"),
            "note": review.get("note"),
            "feedback": review.get("feedback"),
            "feedback_reason": review.get("feedback_reason"),
            "feedback_note": review.get("feedback_note"),
            "application": apps.get(cluster_id),
            "company_applications": [
                a for a in by_company.get(str(best.get("company_norm") or ""), [])
                if a.get("cluster_id") != cluster_id
            ],
        })
        for key in ("filter_reasons", "reasons_plus", "reasons_minus", "matched_skills",
                    "missing_skills", "role_labels", "skills", "countries", "cities",
                    "remote_regions", "skills_nice",
                    "employment_types", "ai_reasons", "languages_required"):
            best[key] = loads(best.get(key))
        try:
            best["components"] = json.loads(best.get("components") or "{}")
        except json.JSONDecodeError:
            best["components"] = {}
        clusters.append(best)
    return Inbox(clusters, previous_run_started(conn))


def filter_reason_counts(inbox: Inbox) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in inbox.view("filtered"):
        for reason in c.get("filter_reasons") or []:
            code = str(reason.get("code") if isinstance(reason, dict) else reason)
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ─────────────────────────── the fast search ─────────────────────────────
#
# RocketApply's search box. `load_inbox` reads every cluster with its full
# description into Python (5 s for 27k vacancies), which is right for a page
# that shows all of them and wrong for a box that should answer as you type.
# This pushes every filter into one SQL statement and never reads a
# description. The one thing SQL cannot do cheaply — pick each vacancy's
# representative when several sources carry it — is done once per data version
# in Python, with `pick_canonical` itself, over only the ~600 clusters that have
# more than one member. Parity with the inbox is a test, not a hope.


@dataclass(frozen=True)
class JobQuery:
    """What the search box and the Filters popover ask for. Empty = no filter."""

    text: str = ""
    countries: tuple[str, ...] = ()        # ISO-2, any of
    roles: tuple[str, ...] = ()            # role labels, any of
    experience: tuple[str, ...] = ()       # intern | entry | mid | senior | unknown
    work_modes: tuple[str, ...] = ()       # remote | hybrid | onsite | unknown
    visa: tuple[str, ...] = ()             # ok | explicit | likely | unknown | no
    posted_within_days: int | None = None
    min_score: float | None = None
    min_salary: float | None = None
    salary_currency: str | None = None     # the minimum applies within one currency
    include_no_salary: bool = True
    lists: tuple[str, ...] = ()            # "target" | "people" — either of
    include_filtered: bool = False         # jobs your hard rules rejected
    include_dismissed: bool = False
    include_closed: bool = False
    hide_tracked: bool = False             # already selected, generated or applied
    sort: str = "score"                    # score | posted | company


@dataclass
class SearchPage:
    rows: list[dict[str, Any]]
    total: int


@dataclass(frozen=True)
class _Shadow:
    """Cluster facts for the ~600 vacancies more than one source carries."""

    hidden: tuple[int, ...]          # non-representative members: never listed
    reopened: tuple[int, ...]        # representative closed, another member open
    on_target: tuple[int, ...]       # representative not on the list, a member is
    on_people: tuple[int, ...]


_SHADOW: dict[str, Any] = {"key": None, "value": None}

_BASE_FROM = """
      FROM jobs j
      JOIN job_matches m            ON m.job_id = j.id
      LEFT JOIN target_companies t  ON t.id = j.target_company_id
      LEFT JOIN companies c         ON c.id = j.company_id
      LEFT JOIN job_review r        ON r.cluster_id = j.cluster_id
"""

_SEARCH_COLUMNS = """
    j.id, j.cluster_id, j.title, j.url, j.location, j.country, j.countries, j.city,
    j.cities, j.remote_status, j.remote_regions, j.role_labels, j.experience_level,
    j.experience_min, j.salary_min, j.salary_max, j.salary_currency, j.salary_text,
    j.visa_status, j.posted_at, j.first_seen_at, j.status, j.agency, j.target_company_id,
    j.company_id, j.posting_company, j.description_truncated,
    COALESCE(t.name, NULLIF(j.posting_company, ''), c.name, '') AS company,
    t.priority AS target_priority, c.name AS networking_company,
    m.passed, m.score, m.label, m.visa_fit, m.ai_score, m.reasons_plus, m.reasons_minus,
    m.filter_reasons, r.dismissed_at, r.seen_at, r.feedback,
    a.id AS application_id, a.status AS application_status
"""

_SORTS = {
    "score": "m.score DESC, j.id",
    "posted": "COALESCE(j.posted_at, j.first_seen_at) DESC, m.score DESC",
    "company": "company COLLATE NOCASE, m.score DESC",
}


def _data_version(conn: sqlite3.Connection) -> str:
    """Changes whenever clusters or scores can have. Only indexed reads: a
    `MAX(last_seen_at)` here cost 0.15 s a search, because it walks every
    posting's description to reach a column stored after it."""
    a = conn.execute("SELECT COUNT(*), MAX(id), MAX(cluster_id) FROM jobs").fetchone()
    b = conn.execute("SELECT COUNT(*), MAX(scored_at) FROM job_matches").fetchone()
    c = conn.execute("SELECT id, status FROM runs WHERE command = 'job-search'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    return json.dumps([list(a), list(b), list(c) if c else None], default=str)


def cluster_shadow(conn: sqlite3.Connection) -> _Shadow:
    """Which rows stand for which vacancy, cached until the data changes."""
    key = _data_version(conn)
    if _SHADOW["key"] == key and _SHADOW["value"] is not None:
        return _SHADOW["value"]
    rows = [dict(r) for r in conn.execute(
        """
        SELECT j.id, j.cluster_id, s.kind AS source_kind, j.description_truncated,
               CASE WHEN length(j.description) > 0 THEN 'x' ELSE '' END AS description,
               j.status, j.last_seen_at, j.target_company_id, j.company_id
          FROM jobs j
          JOIN job_matches m ON m.job_id = j.id
          LEFT JOIN job_sources s ON s.id = j.source_id
         WHERE j.cluster_id IN (
               SELECT j2.cluster_id FROM jobs j2 JOIN job_matches m2 ON m2.job_id = j2.id
                WHERE j2.cluster_id IS NOT NULL
                GROUP BY j2.cluster_id HAVING COUNT(*) > 1)
        """
    )]
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["cluster_id"]), []).append(row)
    hidden: list[int] = []
    reopened: list[int] = []
    on_target: list[int] = []
    on_people: list[int] = []
    for members in grouped.values():
        best = pick_canonical(members)
        bid = int(best["id"])
        hidden += [int(m["id"]) for m in members if int(m["id"]) != bid]
        if best["status"] != "open" and any(m["status"] == "open" for m in members):
            reopened.append(bid)
        if not best.get("target_company_id") and any(m.get("target_company_id") for m in members):
            on_target.append(bid)
        if not best.get("company_id") and any(m.get("company_id") for m in members):
            on_people.append(bid)
    value = _Shadow(tuple(hidden), tuple(reopened), tuple(on_target), tuple(on_people))
    _SHADOW.update(key=key, value=value)
    return value


def _like(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(q: JobQuery, shadow: _Shadow, *, skip: str | None = None
           ) -> tuple[str, dict[str, Any]]:
    """The WHERE clause for `q`, leaving out one dimension for its own facet.

    Lists are bound as JSON arrays and unpacked with `json_each`, so the SQL
    text never depends on how many values were chosen.
    """
    # `m.job_id`, not `j.id`: the same value, but naming the matches table keeps
    # SQLite starting from its small `passed` index instead of scanning `jobs`,
    # whose rows carry every description — 0.13 s against 0.03 s.
    clauses = ["j.cluster_id IS NOT NULL",
               "m.job_id NOT IN (SELECT value FROM json_each(:hidden))"]
    params: dict[str, Any] = {"hidden": json.dumps(shadow.hidden)}
    if not q.include_closed:
        # Unary `+` keeps SQLite off `ix_jobs_status`, for the same reason as above.
        clauses.append("(+j.status = 'open' OR j.id IN (SELECT value FROM json_each(:reopened)))")
        params["reopened"] = json.dumps(shadow.reopened)
    if not q.include_filtered:
        clauses.append("m.passed = 1")
    if not q.include_dismissed:
        clauses.append("r.dismissed_at IS NULL")
    if q.hide_tracked:
        clauses.append("NOT EXISTS (SELECT 1 FROM applications x WHERE x.cluster_id ="
                       " j.cluster_id AND x.status != 'SKIPPED')")
    tokens = [t for t in (q.text or "").lower().split() if t][:6]
    for i, token in enumerate(tokens):
        key = f"t{i}"
        params[key] = _like(token)
        clauses.append(
            f"(lower(j.title) LIKE :{key} ESCAPE '\\' OR lower(j.posting_company) LIKE :{key}"
            f" ESCAPE '\\' OR lower(t.name) LIKE :{key} ESCAPE '\\' OR lower(c.name) LIKE"
            f" :{key} ESCAPE '\\' OR lower(j.location) LIKE :{key} ESCAPE '\\'"
            f" OR lower(j.skills) LIKE :{key} ESCAPE '\\')"
        )
    if q.countries and skip != "countries":
        params["countries"] = json.dumps(list(q.countries))
        clauses.append("EXISTS (SELECT 1 FROM json_each(j.countries) jc WHERE jc.value IN"
                       " (SELECT value FROM json_each(:countries)))")
    if q.roles and skip != "roles":
        params["roles"] = json.dumps(list(q.roles))
        clauses.append("EXISTS (SELECT 1 FROM json_each(j.role_labels) jr WHERE jr.value IN"
                       " (SELECT value FROM json_each(:roles)))")
    if q.experience and skip != "experience":
        params["experience"] = json.dumps(list(q.experience))
        clauses.append("COALESCE(j.experience_level, 'unknown') IN"
                       " (SELECT value FROM json_each(:experience))")
    if q.work_modes and skip != "work_modes":
        params["modes"] = json.dumps(list(q.work_modes))
        clauses.append("COALESCE(j.remote_status, 'unknown') IN"
                       " (SELECT value FROM json_each(:modes))")
    if q.visa and skip != "visa":
        params["visa"] = json.dumps(list(q.visa))
        clauses.append("COALESCE(m.visa_fit, 'unknown') IN (SELECT value FROM json_each(:visa))")
    if q.posted_within_days and skip != "posted":
        params["window"] = f"-{int(q.posted_within_days)} days"
        clauses.append("COALESCE(j.posted_at, j.first_seen_at) >= datetime('now', :window)")
    if q.min_score is not None and skip != "score":
        params["min_score"] = float(q.min_score)
        clauses.append("m.score >= :min_score")
    if q.min_salary is not None and skip != "salary":
        params["min_salary"] = float(q.min_salary)
        params["currency"] = (q.salary_currency or "").upper()
        with_salary = "(j.salary_min >= :min_salary AND upper(j.salary_currency) = :currency)"
        clauses.append(f"({with_salary} OR j.salary_min IS NULL)" if q.include_no_salary
                       else with_salary)
    if q.lists and skip != "lists":
        either = []
        if "target" in q.lists:
            params["on_target"] = json.dumps(shadow.on_target)
            either.append("j.target_company_id IS NOT NULL OR j.id IN"
                          " (SELECT value FROM json_each(:on_target))")
        if "people" in q.lists:
            params["on_people"] = json.dumps(shadow.on_people)
            either.append("j.company_id IS NOT NULL OR j.id IN"
                          " (SELECT value FROM json_each(:on_people))")
        if either:
            clauses.append("(" + " OR ".join(either) + ")")
    return " AND ".join(clauses), params


def _search_row(row: sqlite3.Row, shadow: _Shadow) -> dict[str, Any]:
    out = dict(row)
    for key in ("countries", "cities", "role_labels", "remote_regions", "reasons_plus",
                "reasons_minus", "filter_reasons"):
        out[key] = loads(out.get(key))
    out["on_target_list"] = bool(out.get("target_company_id")) or out["id"] in shadow.on_target
    out["on_people_list"] = bool(out.get("company_id")) or out["id"] in shadow.on_people
    if out["id"] in shadow.reopened:
        out["status"] = "open"
    return out


def search_jobs(conn: sqlite3.Connection, q: JobQuery | None = None, *,
                limit: int = 100, offset: int = 0) -> SearchPage:
    """One page of vacancies — one row per cluster — best first, no descriptions."""
    q = q or JobQuery()
    shadow = cluster_shadow(conn)
    where, params = _where(q, shadow)
    total = int(conn.execute(f"SELECT COUNT(*) {_BASE_FROM} WHERE {where}", params
                             ).fetchone()[0])
    order = _SORTS.get(q.sort, _SORTS["score"])
    sql = f"""
        SELECT {_SEARCH_COLUMNS}
          {_BASE_FROM}
          LEFT JOIN applications a ON a.id = (
               SELECT MAX(id) FROM applications WHERE cluster_id = j.cluster_id)
         WHERE {where}
         ORDER BY {order}
         LIMIT :limit OFFSET :offset
    """
    params.update(limit=int(limit), offset=int(offset))
    return SearchPage([_search_row(r, shadow) for r in conn.execute(sql, params)], total)


def search_ids(conn: sqlite3.Connection, q: JobQuery, *, limit: int) -> list[int]:
    """The top `limit` job ids for `q` — what "select the top N" selects."""
    shadow = cluster_shadow(conn)
    where, params = _where(q, shadow)
    order = _SORTS.get(q.sort, _SORTS["score"])
    params["limit"] = int(limit)
    return [int(r[0]) for r in conn.execute(
        f"SELECT j.id {_BASE_FROM} WHERE {where} ORDER BY {order} LIMIT :limit", params)]


def search_facets(conn: sqlite3.Connection, q: JobQuery | None = None
                  ) -> dict[str, dict[str, int]]:
    """How many results each filter option would give, with the rest of the
    query applied. Each dimension leaves its own filter out, so choosing
    Germany does not make every other country read zero."""
    q = q or JobQuery()
    shadow = cluster_shadow(conn)
    out: dict[str, dict[str, int]] = {}

    def grouped(dim: str, expr: str, join: str = "") -> dict[str, int]:
        where, params = _where(q, shadow, skip=dim)
        sql = (f"SELECT {expr} AS v, COUNT(*) AS n {_BASE_FROM} {join} WHERE {where}"
               " GROUP BY v ORDER BY n DESC")
        return {str(r["v"]): int(r["n"]) for r in conn.execute(sql, params) if r["v"] is not None}

    out["countries"] = grouped("countries", "jc2.value", ", json_each(j.countries) jc2")
    out["roles"] = grouped("roles", "jr2.value", ", json_each(j.role_labels) jr2")
    out["experience"] = grouped("experience", "COALESCE(j.experience_level, 'unknown')")
    out["work_modes"] = grouped("work_modes", "COALESCE(j.remote_status, 'unknown')")
    out["visa"] = grouped("visa", "COALESCE(m.visa_fit, 'unknown')")
    out["label"] = grouped("score", "m.label")
    where, params = _where(q, shadow, skip="posted")
    windows = (1, 3, 7, 14, 30)
    cases = ", ".join(
        f"SUM(CASE WHEN COALESCE(j.posted_at, j.first_seen_at) >= datetime('now', '-{d} days')"
        f" THEN 1 ELSE 0 END) AS d{d}" for d in windows)
    r = conn.execute(f"SELECT {cases} {_BASE_FROM} WHERE {where}", params).fetchone()
    out["posted"] = {str(d): int(r[f"d{d}"] or 0) for d in windows}
    where, params = _where(q, shadow, skip="lists")
    params.update(on_target=json.dumps(shadow.on_target), on_people=json.dumps(shadow.on_people))
    r = conn.execute(
        f"""SELECT SUM(CASE WHEN j.target_company_id IS NOT NULL OR j.id IN
                   (SELECT value FROM json_each(:on_target)) THEN 1 ELSE 0 END) AS target,
                   SUM(CASE WHEN j.company_id IS NOT NULL OR j.id IN
                   (SELECT value FROM json_each(:on_people)) THEN 1 ELSE 0 END) AS people
            {_BASE_FROM} WHERE {where}""", params).fetchone()
    out["lists"] = {"target": int(r["target"] or 0), "people": int(r["people"] or 0)}
    where, params = _where(q, shadow, skip="salary")
    out["currencies"] = {
        str(r["v"]): int(r["n"]) for r in conn.execute(
            f"SELECT upper(j.salary_currency) AS v, COUNT(*) AS n {_BASE_FROM} WHERE {where}"
            " AND j.salary_min IS NOT NULL AND j.salary_currency IS NOT NULL"
            " AND j.salary_currency != '' GROUP BY v ORDER BY n DESC", params)
    }
    return out


def stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The process strip: found → selected → documents ready → in process → applied."""
    found = search_jobs(conn, JobQuery(), limit=0).total
    counts = {r["status"]: int(r["n"]) for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}
    submitted = ("APPLIED", "ONLINE_ASSESSMENT", "INTERVIEW", "FINAL_INTERVIEW", "OFFER",
                 "REJECTED", "WITHDRAWN")
    return {
        "found": found,
        "selected": counts.get("SHORTLISTED", 0),
        "docs_ready": counts.get("CV_READY", 0),
        "in_process": counts.get("IN_PROCESS", 0),
        "applied": sum(counts.get(s, 0) for s in submitted),
    }


def job_detail(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    """Everything about one vacancy, description included — the detail panel."""
    r = conn.execute(
        """
        SELECT j.*, s.name AS source_name, s.kind AS source_kind,
               COALESCE(t.name, NULLIF(j.posting_company, ''), c.name, '') AS company,
               t.priority AS target_priority, c.name AS networking_company,
               m.passed, m.score, m.label, m.components, m.reasons_plus, m.reasons_minus,
               m.filter_reasons, m.matched_skills, m.missing_skills, m.visa_fit,
               m.ai_score, m.ai_reasons, m.scored_at
          FROM jobs j
          LEFT JOIN job_matches m ON m.job_id = j.id
          LEFT JOIN job_sources s ON s.id = j.source_id
          LEFT JOIN target_companies t ON t.id = j.target_company_id
          LEFT JOIN companies c ON c.id = j.company_id
         WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if r is None:
        return None
    out = dict(r)
    for key in ("countries", "cities", "role_labels", "remote_regions", "reasons_plus",
                "reasons_minus", "filter_reasons", "matched_skills", "missing_skills",
                "skills", "skills_nice", "employment_types", "ai_reasons", "languages_required"):
        out[key] = loads(out.get(key))
    try:
        out["components"] = json.loads(out.get("components") or "{}")
    except json.JSONDecodeError:
        out["components"] = {}
    try:
        langs = conn.execute("SELECT missing_languages FROM job_matches WHERE job_id = ?",
                             (job_id,)).fetchone()
        out["missing_languages"] = loads(langs[0]) if langs else []
    except sqlite3.OperationalError:     # a database from before v10
        out["missing_languages"] = []
    out["members"] = [dict(m) for m in conn.execute(
        """
        SELECT j.id, j.url, j.status, s.name AS source, s.kind
          FROM jobs j LEFT JOIN job_sources s ON s.id = j.source_id
         WHERE j.cluster_id = ? ORDER BY j.id
        """,
        (out.get("cluster_id"),),
    )] if out.get("cluster_id") is not None else []
    out["members"].sort(key=lambda m: source_rank(m.get("kind")))
    app = conn.execute(
        "SELECT * FROM applications WHERE cluster_id = ? ORDER BY id DESC LIMIT 1",
        (out.get("cluster_id"),),
    ).fetchone()
    out["application"] = dict(app) if app else None
    review = conn.execute("SELECT * FROM job_review WHERE cluster_id = ?",
                          (out.get("cluster_id"),)).fetchone()
    out["review"] = dict(review) if review else None
    return out
