"""The application tracker — §37-39 of the spec.

An application row is created the moment the owner acts on a vacancy — Save
makes it SHORTLISTED, "Mark applied" makes it APPLIED — or by hand for a job
the search never saw. Every status change is also written to
`application_events`, so the history is there when the analytics of §80 want
it, and nothing is ever deleted (§43: historical records must remain).

§39's duplicate check lives here too: before applying, "have I already applied
to this company / this job?" — answered across sources, because it is keyed on
the cluster and on the normalised company name, not on one source's URL.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .companies import normalize_company

STATUSES = (
    "SHORTLISTED", "CV_READY", "APPLIED", "ONLINE_ASSESSMENT", "INTERVIEW",
    "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN", "CLOSED",
)
#: Statuses that mean the application went out.
SUBMITTED = frozenset({"APPLIED", "ONLINE_ASSESSMENT", "INTERVIEW", "FINAL_INTERVIEW",
                       "OFFER", "REJECTED", "WITHDRAWN"})
EDITABLE = (
    "company_name", "title", "location", "date_applied", "cv_version",
    "cover_letter_version", "application_url", "contact_person", "notes", "salary",
    "response_date", "next_action", "next_action_date",
)


def _today(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT date('now')").fetchone()[0])


def _event(conn: sqlite3.Connection, app_id: int, old: str | None, new: str,
           note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO application_events (application_id, from_status, to_status, note)"
        " VALUES (?,?,?,?)",
        (app_id, old, new, note),
    )


def get(conn: sqlite3.Connection, app_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    return dict(r) if r else None


def for_cluster(conn: sqlite3.Connection, cluster_id: int) -> dict[str, Any] | None:
    r = conn.execute(
        "SELECT * FROM applications WHERE cluster_id = ? ORDER BY id DESC LIMIT 1",
        (cluster_id,),
    ).fetchone()
    return dict(r) if r else None


def at_company(conn: sqlite3.Connection, company: str) -> list[dict[str, Any]]:
    """§39 — every application to this employer, however it was spelled."""
    norm = normalize_company(company)
    if not norm:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM applications WHERE company_norm = ? ORDER BY COALESCE(date_applied,"
        " created_at) DESC", (norm,),
    )]


def from_cluster(conn: sqlite3.Connection, cluster: dict[str, Any], status: str = "SHORTLISTED",
                 *, note: str | None = None) -> int:
    """Start tracking a vacancy from the inbox, or move the existing record on.

    Idempotent per vacancy: saving a job twice does not make two applications,
    and "Mark applied" on a saved job advances it rather than duplicating it.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    existing = for_cluster(conn, int(cluster["cluster_id"]))
    if existing:
        if existing["status"] != status:
            set_status(conn, int(existing["id"]), status, note=note)
        return int(existing["id"])
    company = cluster.get("company") or cluster.get("posting_company") or "unknown"
    cur = conn.execute(
        """
        INSERT INTO applications (cluster_id, job_id, target_company_id, company_name,
            company_norm, title, location, status, date_applied, application_url, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (int(cluster["cluster_id"]), cluster.get("id"), cluster.get("target_company_id"),
         company, normalize_company(company), cluster.get("title") or "",
         cluster.get("location"), status,
         _today(conn) if status in SUBMITTED else None,
         cluster.get("url"), note),
    )
    app_id = int(cur.lastrowid or 0)
    _event(conn, app_id, None, status, note)
    return app_id


def create_manual(conn: sqlite3.Connection, *, company: str, title: str,
                  status: str = "APPLIED", date_applied: str | None = None,
                  application_url: str | None = None, location: str | None = None,
                  notes: str | None = None) -> int:
    """An application the search never saw — applied through LinkedIn, a
    referral, a career fair. It still counts for §39's duplicate check."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if not company.strip() or not title.strip():
        raise ValueError("an application needs a company and a title")
    cur = conn.execute(
        """
        INSERT INTO applications (company_name, company_norm, title, location, status,
            date_applied, application_url, notes)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (company.strip(), normalize_company(company), title.strip(), location, status,
         date_applied or (_today(conn) if status in SUBMITTED else None),
         application_url, notes),
    )
    app_id = int(cur.lastrowid or 0)
    _event(conn, app_id, None, status, "added by hand")
    return app_id


def set_status(conn: sqlite3.Connection, app_id: int, status: str,
               *, note: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    app = get(conn, app_id)
    if app is None:
        raise ValueError(f"no application {app_id}")
    if app["status"] == status:
        return
    fields: dict[str, Any] = {"status": status}
    if status in SUBMITTED and not app.get("date_applied"):
        fields["date_applied"] = _today(conn)
    if app["status"] in SUBMITTED and status in {"REJECTED", "OFFER", "INTERVIEW",
                                                  "ONLINE_ASSESSMENT", "FINAL_INTERVIEW"} \
            and not app.get("response_date"):
        fields["response_date"] = _today(conn)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE applications SET {assignments}, updated_at = datetime('now')"
                 " WHERE id = ?", (*fields.values(), app_id))
    _event(conn, app_id, app["status"], status, note)


def update(conn: sqlite3.Connection, app_id: int, **fields: Any) -> None:
    """Edit the free fields. Status goes through `set_status`, for the history."""
    clean = {k: (v if v not in ("",) else None) for k, v in fields.items() if k in EDITABLE}
    if "company_name" in clean and clean["company_name"]:
        clean["company_norm"] = normalize_company(clean["company_name"])
    if not clean:
        return
    assignments = ", ".join(f"{k} = ?" for k in clean)
    conn.execute(f"UPDATE applications SET {assignments}, updated_at = datetime('now')"
                 " WHERE id = ?", (*clean.values(), app_id))


def all_applications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT a.*, t.priority AS target_priority,
               (SELECT MAX(at) FROM application_events e WHERE e.application_id = a.id)
                   AS last_change
          FROM applications a
          LEFT JOIN target_companies t ON t.id = a.target_company_id
         ORDER BY CASE WHEN a.next_action_date IS NULL THEN 1 ELSE 0 END,
                  a.next_action_date, a.updated_at DESC
        """
    )]


def events(conn: sqlite3.Connection, app_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM application_events WHERE application_id = ? ORDER BY id", (app_id,)
    )]


def due(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Next actions due today or overdue."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM applications WHERE next_action_date IS NOT NULL"
        " AND next_action_date <= date('now') AND status NOT IN ('REJECTED','WITHDRAWN',"
        "'CLOSED','OFFER') ORDER BY next_action_date"
    )]
