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
    "salary_min", "salary_max", "salary_currency", "salary_text",
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
            visa_fit, scored_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(job_id) DO UPDATE SET
            config_hash=excluded.config_hash, passed=excluded.passed,
            filter_reasons=excluded.filter_reasons, score=excluded.score,
            label=excluded.label, components=excluded.components,
            reasons_plus=excluded.reasons_plus, reasons_minus=excluded.reasons_minus,
            matched_skills=excluded.matched_skills, missing_skills=excluded.missing_skills,
            visa_fit=excluded.visa_fit, scored_at=excluded.scored_at
        """,
        (job_id, config_hash, int(passed), json.dumps(reasons, ensure_ascii=False),
         match.score, match.label, json.dumps(components),
         json.dumps(match.plus, ensure_ascii=False), json.dumps(match.minus, ensure_ascii=False),
         json.dumps(match.matched_skills), json.dumps(match.missing_skills), match.visa_fit),
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
    _review(conn, int(cluster_id), dismissed_at=_now(conn), dismissed_reason=reason,
            seen_at=_now(conn))


def restore(conn: sqlite3.Connection, cluster_id: int) -> None:
    _review(conn, int(cluster_id), dismissed_at=None, dismissed_reason=None)


def set_note(conn: sqlite3.Connection, cluster_id: int, note: str | None) -> None:
    _review(conn, int(cluster_id), note=(note or "").strip() or None)


def set_feedback(conn: sqlite3.Connection, cluster_id: int, feedback: str | None) -> None:
    """§91: good_match / bad_match, for measuring whether ranking improves."""
    _review(conn, int(cluster_id), feedback=feedback)


def _now(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT datetime('now')").fetchone()[0])


# ─────────────────────────────── reading ─────────────────────────────────

_INBOX_SQL = """
    SELECT j.id, j.cluster_id, j.title, j.posting_company, j.company_id, j.target_company_id,
           j.company_norm, j.url, j.canonical_url, j.location, j.country, j.countries,
           j.city, j.cities, j.remote_status, j.remote_region, j.employment_types,
           j.experience_min, j.experience_max, j.experience_level, j.experience_required,
           j.experience_evidence, j.education_required, j.education_preferred,
           j.salary_min, j.salary_max, j.salary_currency, j.salary_text,
           j.visa_status, j.visa_evidence, j.visa_scope, j.relocation, j.role_labels,
           j.skills, j.description, j.description_truncated, j.posted_at,
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
            "application": apps.get(cluster_id),
            "company_applications": [
                a for a in by_company.get(str(best.get("company_norm") or ""), [])
                if a.get("cluster_id") != cluster_id
            ],
        })
        for key in ("filter_reasons", "reasons_plus", "reasons_minus", "matched_skills",
                    "missing_skills", "role_labels", "skills", "countries", "cities",
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
