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

import logging
import pathlib
import sqlite3
from typing import Any

from .companies import normalize_company

log = logging.getLogger(__name__)

STATUSES = (
    "SHORTLISTED", "CV_READY", "IN_PROCESS", "APPLIED", "ONLINE_ASSESSMENT", "INTERVIEW",
    "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN", "CLOSED", "SKIPPED",
)
#: RocketApply's pipeline, in order. IN_PROCESS = exported to a CSV to work
#: through; SKIPPED = taken off the shortlist (nothing is ever deleted, §43).
#: Everything submitted ranks above IN_PROCESS, so `advance` never pulls an
#: application that went out back to "documents ready".
PIPELINE_ORDER = {"SKIPPED": -1, "SHORTLISTED": 0, "CV_READY": 1, "IN_PROCESS": 2}

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
    if status in SUBMITTED and app["status"] not in SUBMITTED:
        file_sent(conn, app_id)


def file_sent(conn: sqlite3.Connection, app_id: int) -> str | None:
    """The application went out: move its folder (CV, letter, everything) from
    `1_ready_to_send/` to `4_already_sent/` and point the application — and any
    export batch that froze the old path — at the new place. Returns the new
    folder, or None when nothing moved.

    Never raises over the files: a folder already moved, deleted by hand, or
    not movable right now must not stop the job being marked applied."""
    app = get(conn, app_id)
    old = (app or {}).get("generation_dir")
    if not old:
        return None
    from ..apply.tidy import file_sent as move_to_sent

    try:
        moved = move_to_sent(pathlib.Path(old))
    except OSError as exc:
        log.warning("could not move %s to already sent: %s", old, exc)
        return None
    if moved is None:
        return None
    new = str(moved.resolve()) if pathlib.Path(old).is_absolute() else str(moved)
    conn.execute("UPDATE applications SET generation_dir = ?, updated_at = datetime('now')"
                 " WHERE generation_dir = ?", (new, old))
    conn.execute("UPDATE export_batch_items SET generation_dir = ? WHERE generation_dir = ?",
                 (new, old))
    return new


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


# ─────────────────────────── RocketApply lifecycle ─────────────────────────
#
# Selected (SHORTLISTED) → documents ready (CV_READY, written when generation
# finishes) → IN_PROCESS (exported to a CSV batch) → APPLIED (a whole batch at
# once) → the outcomes above. The database is the source of truth: a selection
# is written the moment it is made, never held in a browser session.


def rank(status: str | None) -> int:
    """Position in the pipeline; every submitted status outranks IN_PROCESS."""
    if status is None:
        return -2
    return PIPELINE_ORDER.get(status, 3)


def cluster_for_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    """What `from_cluster` needs, for a posting known only by its `jobs.id`.

    The company is named the way the inbox names it — the target list's name,
    else the posting's, else the networking list's — so "already applied to
    this employer" (§39) matches across both entry points.
    """
    r = conn.execute(
        """
        SELECT j.id, j.cluster_id, j.title, j.location, j.url, j.target_company_id,
               COALESCE(t.name, NULLIF(j.posting_company, ''), c.name, '') AS company
          FROM jobs j
          LEFT JOIN target_companies t ON t.id = j.target_company_id
          LEFT JOIN companies c ON c.id = j.company_id
         WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if r is None:
        return None
    out = dict(r)
    # A posting that dedupe has not reached yet is its own vacancy.
    out["cluster_id"] = out["cluster_id"] if out["cluster_id"] is not None else -int(job_id)
    return out


def advance(conn: sqlite3.Connection, app_id: int, status: str,
            *, note: str | None = None) -> bool:
    """Move forward only. Returns False when the application is already at or
    past `status` — regenerating a CV must not un-apply a job."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    app = get(conn, app_id)
    if app is None:
        raise ValueError(f"no application {app_id}")
    current = app["status"]
    if current == "SKIPPED" or rank(status) > rank(current):
        set_status(conn, app_id, status, note=note)
        return True
    return False


def ensure_for_job(conn: sqlite3.Connection, job_id: int, status: str = "SHORTLISTED",
                   *, note: str | None = None) -> int:
    """The application for this posting, created if needed and moved forward to
    `status` if it is behind. Never moves one backwards."""
    cluster = cluster_for_job(conn, job_id)
    if cluster is None:
        raise ValueError(f"no job {job_id}")
    existing = for_cluster(conn, int(cluster["cluster_id"]))
    if existing is None:
        return from_cluster(conn, cluster, status, note=note)
    advance(conn, int(existing["id"]), status, note=note)
    return int(existing["id"])


def select_jobs(conn: sqlite3.Connection, job_ids: list[int]) -> list[int]:
    """Search → Selected, for many postings at once. A SKIPPED one comes back, and
    so does a dismissed one: selecting it again is the undo."""
    out = []
    for job_id in job_ids:
        cluster = cluster_for_job(conn, int(job_id))
        if cluster is not None:
            conn.execute("UPDATE job_review SET dismissed_at = NULL, dismissed_reason = NULL,"
                         " updated_at = datetime('now') WHERE cluster_id = ?",
                         (int(cluster["cluster_id"]),))
        out.append(ensure_for_job(conn, int(job_id), "SHORTLISTED", note="selected"))
    return out


def skip(conn: sqlite3.Connection, app_id: int, *, note: str | None = None) -> None:
    """Off the shortlist, kept on record."""
    set_status(conn, app_id, "SKIPPED", note=note or "removed from the selection")


def skip_cluster(conn: sqlite3.Connection, cluster_id: int, *, note: str | None = None) -> int:
    """Every application for this vacancy that has not gone out yet, off the
    pipeline. Returns how many moved."""
    rows = conn.execute(
        "SELECT id FROM applications WHERE cluster_id = ? AND status IN"
        " ('SHORTLISTED', 'CV_READY', 'IN_PROCESS')", (int(cluster_id),)).fetchall()
    for r in rows:
        skip(conn, int(r["id"]), note=note)
    return len(rows)


def is_dismissed(conn: sqlite3.Connection, job_id: int) -> bool:
    cluster = cluster_for_job(conn, int(job_id))
    return cluster is not None and conn.execute(
        "SELECT 1 FROM job_review WHERE cluster_id = ? AND dismissed_at IS NOT NULL",
        (int(cluster["cluster_id"]),)).fetchone() is not None


def record_generation(conn: sqlite3.Connection, *, job_id: int, directory: str | None,
                      cv_version: str | None = None, letter_version: str | None = None,
                      letter_state: str = "", mode: str = "cv_letter") -> int:
    """A CV (and maybe a letter) exists for this posting: remember where, and
    move the application to CV_READY. Clears any earlier review, because the
    documents it approved are not these ones.

    A job dismissed while its documents were being made stays dismissed: the
    files are remembered, the status does not move. Returns 0 when there is no
    application to remember them on."""
    dismissed = is_dismissed(conn, job_id)
    if dismissed:
        cluster = cluster_for_job(conn, job_id)
        existing = for_cluster(conn, int(cluster["cluster_id"])) if cluster else None
        if existing is None:
            return 0
        app_id = int(existing["id"])
    else:
        app_id = ensure_for_job(conn, job_id, "SHORTLISTED")
    conn.execute(
        """
        UPDATE applications SET generation_dir = ?, generation_mode = ?,
               generated_at = datetime('now'), letter_state = ?, reviewed_at = NULL,
               review_note = NULL, cv_version = COALESCE(?, cv_version),
               cover_letter_version = COALESCE(?, cover_letter_version),
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (directory, mode, letter_state or "", cv_version, letter_version, app_id),
    )
    if not dismissed:
        advance(conn, app_id, "CV_READY", note=f"documents generated ({mode})")
    return app_id


def set_generation(conn: sqlite3.Connection, app_id: int, *, directory: str,
                   letter_state: str, letter_version: str | None = None) -> None:
    """The documents moved or changed (a hand edit re-filed them): point the
    application at them again. Any approval is taken back — it was of the old
    text."""
    conn.execute(
        "UPDATE applications SET generation_dir = ?, letter_state = ?,"
        " cover_letter_version = COALESCE(?, cover_letter_version), reviewed_at = NULL,"
        " updated_at = datetime('now') WHERE id = ?",
        (directory, letter_state, letter_version, app_id),
    )


def set_review(conn: sqlite3.Connection, app_id: int, *, approved: bool,
               note: str | None = None) -> None:
    """Approve the generated documents (or take the approval back)."""
    conn.execute(
        "UPDATE applications SET reviewed_at = CASE WHEN ? THEN datetime('now') END,"
        " review_note = ?, updated_at = datetime('now') WHERE id = ?",
        (int(approved), note, app_id),
    )


def create_export_batch(conn: sqlite3.Connection, app_ids: list[int], *, name: str,
                        csv_path: str | None = None, note: str | None = None) -> int:
    """Freeze these applications into a named CSV batch and move them to
    IN_PROCESS. The folder each one pointed at is snapshotted, so the CSV and
    the batch agree even if a job is regenerated later."""
    if not app_ids:
        raise ValueError("an export batch needs at least one application")
    if not name.strip():
        raise ValueError("an export batch needs a name")
    cur = conn.execute(
        "INSERT INTO export_batches (name, csv_path, note) VALUES (?,?,?)",
        (name.strip(), csv_path, note),
    )
    batch_id = int(cur.lastrowid or 0)
    for position, app_id in enumerate(app_ids, start=1):
        app = get(conn, int(app_id))
        if app is None:
            raise ValueError(f"no application {app_id}")
        conn.execute(
            "INSERT INTO export_batch_items (batch_id, application_id, position, generation_dir)"
            " VALUES (?,?,?,?)",
            (batch_id, int(app_id), position, app.get("generation_dir")),
        )
        advance(conn, int(app_id), "IN_PROCESS", note=f"exported in {name.strip()}")
    return batch_id


def set_batch_csv(conn: sqlite3.Connection, batch_id: int, csv_path: str) -> None:
    conn.execute("UPDATE export_batches SET csv_path = ? WHERE id = ?", (csv_path, batch_id))


def mark_batch_applied(conn: sqlite3.Connection, batch_id: int, *,
                       exclude: tuple[int, ...] | list[int] = ()) -> int:
    """Every application in the batch went out — except the ones in `exclude`,
    which stay IN_PROCESS. Returns how many moved. Sets `date_applied` (via
    `set_status`) and stamps the batch."""
    batch = conn.execute("SELECT * FROM export_batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError(f"no export batch {batch_id}")
    skip_ids = {int(a) for a in exclude}
    moved = 0
    for item in batch_items(conn, batch_id):
        app_id = int(item["application_id"])
        if app_id in skip_ids:
            continue
        if advance(conn, app_id, "APPLIED", note=f"batch {batch['name']}"):
            moved += 1
    conn.execute("UPDATE export_batches SET applied_at = datetime('now') WHERE id = ?",
                 (batch_id,))
    return moved


def batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every export batch, newest first, with how many of it went out."""
    return [dict(r) for r in conn.execute(
        """
        SELECT b.*, COUNT(i.application_id) AS size,
               SUM(CASE WHEN a.status = 'IN_PROCESS' THEN 1 ELSE 0 END) AS in_process,
               SUM(CASE WHEN a.status NOT IN ('IN_PROCESS', 'CV_READY', 'SHORTLISTED',
                   'SKIPPED', 'CLOSED') THEN 1 ELSE 0 END) AS applied
          FROM export_batches b
          LEFT JOIN export_batch_items i ON i.batch_id = b.id
          LEFT JOIN applications a ON a.id = i.application_id
         GROUP BY b.id
         ORDER BY b.id DESC
        """
    )]


def batch_items(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT i.position, i.application_id, i.generation_dir AS exported_dir, a.*
          FROM export_batch_items i JOIN applications a ON a.id = i.application_id
         WHERE i.batch_id = ?
         ORDER BY i.position
        """,
        (batch_id,),
    )]


def batch_of(conn: sqlite3.Connection, app_id: int) -> dict[str, Any] | None:
    """The latest export batch an application went out in."""
    r = conn.execute(
        "SELECT b.* FROM export_batch_items i JOIN export_batches b ON b.id = i.batch_id"
        " WHERE i.application_id = ? ORDER BY b.id DESC LIMIT 1",
        (app_id,),
    ).fetchone()
    return dict(r) if r else None


def exported_dirs(conn: sqlite3.Connection) -> set[str]:
    """Application folders a CSV has pointed at, as stored (absolute paths).
    `apply tidy` leaves these where they are."""
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT generation_dir FROM export_batch_items WHERE generation_dir IS NOT NULL"
        )
    }


def by_status(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """Applications in these statuses, with their posting's score, best first."""
    marks = ",".join("?" * len(statuses))
    return [dict(r) for r in conn.execute(
        f"""
        SELECT a.*, m.score, m.label, m.reasons_plus, m.reasons_minus, j.url AS job_url,
               j.posted_at, j.country, j.remote_status
          FROM applications a
          LEFT JOIN job_matches m ON m.job_id = a.job_id
          LEFT JOIN jobs j ON j.id = a.job_id
         WHERE a.status IN ({marks})
         ORDER BY COALESCE(m.score, 0) DESC, a.id
        """,
        statuses,
    )]
