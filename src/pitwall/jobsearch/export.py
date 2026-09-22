"""CSV out — §77-78 of the spec. Usable without the app, so nothing is locked in.

One row per vacancy (cluster), with the §77 columns first and the optional
ones after. Application history exports separately, with its full status log
flattened into one readable column.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any

from .locations import country_name
from .store import Inbox, load_inbox
from .tracker import all_applications, events

JOB_COLUMNS = (
    "job_id", "company", "title", "country", "city", "remote", "employment_type",
    "experience", "salary", "visa_status", "match_score", "role_match", "skill_match",
    "career_match", "company_priority", "source", "application_url", "date_found", "status",
    # optional (§77)
    "match_label", "matched_skills", "missing_skills", "why_matched", "concerns",
    "filtered_out_because", "all_sources", "notes", "ai_fit",
)

APPLICATION_COLUMNS = (
    "application_id", "company", "title", "location", "status", "date_applied",
    "cv_version", "cover_letter_version", "application_url", "contact_person", "salary",
    "response_date", "next_action", "next_action_date", "notes", "history",
)


def _experience(c: dict[str, Any]) -> str:
    lo, hi = c.get("experience_min"), c.get("experience_max")
    if lo is not None:
        span = f"{float(lo):g}-{float(hi):g}" if hi is not None else f"{float(lo):g}+"
        return f"{span} years" + ("" if c.get("experience_required") else " (preferred)")
    return c.get("experience_level") or "unknown"


def _salary(c: dict[str, Any]) -> str:
    lo, hi, cur = c.get("salary_min"), c.get("salary_max"), c.get("salary_currency") or ""
    if lo is None and hi is None:
        return "unknown"
    return f"{cur} {int(lo or 0):,}-{int(hi or 0):,}".strip()


def _component(c: dict[str, Any], name: str) -> str:
    comp = (c.get("components") or {}).get(name) or {}
    return str(comp.get("label") or "")


def _status(c: dict[str, Any]) -> str:
    app = c.get("application")
    if app:
        return str(app["status"])
    if c.get("dismissed_at"):
        return f"DISMISSED ({c.get('dismissed_reason') or 'no reason'})"
    if c["status"] != "open":
        return "CLOSED"
    return "REVIEW" if c.get("seen_at") else "DISCOVERED"


def job_rows(inbox: Inbox, view: str = "all") -> list[dict[str, Any]]:
    out = []
    for c in inbox.view(view):
        out.append({
            "job_id": c["cluster_id"],
            "company": c.get("company") or "",
            "title": c.get("title") or "",
            "country": ", ".join(country_name(x) for x in c.get("countries") or []),
            "city": ", ".join(c.get("cities") or []),
            "remote": c.get("remote_status") or "unknown",
            "employment_type": ", ".join(c.get("employment_types") or []),
            "experience": _experience(c),
            "salary": _salary(c),
            "visa_status": c.get("visa_status") or "UNKNOWN",
            "match_score": c.get("score"),
            "role_match": _component(c, "role"),
            "skill_match": _component(c, "skills"),
            "career_match": _component(c, "career"),
            "company_priority": c.get("target_priority") or "",
            "source": c.get("source_name") or "",
            "application_url": c.get("url") or "",
            "date_found": (c.get("first_seen") or "")[:10],
            "status": _status(c),
            "match_label": c.get("label") or "",
            "matched_skills": ", ".join(c.get("matched_skills") or []),
            "missing_skills": ", ".join(c.get("missing_skills") or []),
            "why_matched": " | ".join(c.get("reasons_plus") or []),
            "concerns": " | ".join(c.get("reasons_minus") or []),
            "filtered_out_because": " | ".join(
                r.get("message", "") if isinstance(r, dict) else str(r)
                for r in c.get("filter_reasons") or []
            ),
            "all_sources": " | ".join(f"{m['source']}: {m['url']}" for m in c.get("members") or []),
            "notes": c.get("note") or "",
            "ai_fit": c.get("ai_score") if c.get("ai_score") is not None else "",
        })
    return out


def _csv(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def jobs_csv(conn: sqlite3.Connection, view: str = "matches") -> str:
    return _csv(job_rows(load_inbox(conn), view), JOB_COLUMNS)


def applications_csv(conn: sqlite3.Connection) -> str:
    rows = []
    for app in all_applications(conn):
        history = " → ".join(
            f"{e['to_status']} {e['at'][:10]}" for e in events(conn, int(app["id"]))
        )
        rows.append({
            "application_id": app["id"],
            "company": app["company_name"],
            "title": app["title"],
            "location": app.get("location") or "",
            "status": app["status"],
            "date_applied": app.get("date_applied") or "",
            "cv_version": app.get("cv_version") or "",
            "cover_letter_version": app.get("cover_letter_version") or "",
            "application_url": app.get("application_url") or "",
            "contact_person": app.get("contact_person") or "",
            "salary": app.get("salary") or "",
            "response_date": app.get("response_date") or "",
            "next_action": app.get("next_action") or "",
            "next_action_date": app.get("next_action_date") or "",
            "notes": app.get("notes") or "",
            "history": history,
        })
    return _csv(rows, APPLICATION_COLUMNS)
