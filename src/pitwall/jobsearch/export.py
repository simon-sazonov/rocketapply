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


# ─────────────────────────── a RocketApply batch ───────────────────────────
#
# What you work through when applying: one row per application in an export
# batch, in the order it was exported, with the link to apply at and the
# ABSOLUTE path of every file to upload. The paths point at the files where they
# already are in `output/` — nothing is copied — and `apply tidy` leaves those
# folders in place, so the CSV stays true.

BATCH_COLUMNS = (
    "#", "company", "role", "location", "score", "why_matched", "concerns", "apply_link",
    "cv_pdf", "letter_pdf", "letter_txt", "cv_docx", "letter_state", "folder", "status",
    "application_id", "job_id",
)


def _reasons(raw: Any) -> str:
    from ..db import loads

    items = raw if isinstance(raw, list) else loads(raw)
    return " | ".join(str(r).lstrip("+?- ").strip() for r in items or [])


#: Job boards you cannot apply through from abroad. MyCareersFuture takes
#: applications only through Singpass, which only Singapore citizens and
#: permanent residents hold — four jobs in the first batch went unapplied
#: because the CSV's link led to a login that could never succeed.
SINGPASS_ONLY = ("mycareersfuture.gov.sg",)


def _route_concern(posting_url: str, apply_link: str) -> str:
    """A warning for a posting whose board you cannot apply through, or ""."""
    if not any(host in (posting_url or "") for host in SINGPASS_ONLY):
        return ""
    if any(host in (apply_link or "") for host in SINGPASS_ONLY):
        return ("MyCareersFuture: applying needs Singpass (Singapore citizens/PRs "
                "only) — find the company's own posting or HR email")
    return "MyCareersFuture needs Singpass — apply_link is the company's own route"


def batch_rows(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    import pathlib

    from ..apply.history import read
    from .tracker import batch_items

    rows = []
    for item in batch_items(conn, batch_id):
        match = conn.execute(
            "SELECT m.score, m.reasons_plus, m.reasons_minus, j.url, j.location"
            " FROM jobs j LEFT JOIN job_matches m ON m.job_id = j.id WHERE j.id = ?",
            (item.get("job_id"),),
        ).fetchone()
        match = dict(match) if match else {}
        folder = item.get("exported_dir") or item.get("generation_dir") or ""
        generation = read(folder) if folder else None

        apply_link = item.get("application_url") or match.get("url") or ""

        def path(value: Any) -> str:
            return str(pathlib.Path(value).resolve()) if value else ""

        rows.append({
            "#": item["position"],
            "company": item.get("company_name") or "",
            "role": item.get("title") or "",
            "location": item.get("location") or match.get("location") or "",
            "score": round(match["score"], 1) if match.get("score") is not None else "",
            "why_matched": _reasons(match.get("reasons_plus")),
            "concerns": " | ".join(c for c in (
                _route_concern(match.get("url") or "", apply_link),
                _reasons(match.get("reasons_minus")),
            ) if c),
            "apply_link": apply_link,
            "cv_pdf": path(generation.cv_pdf) if generation else "",
            "letter_pdf": path(generation.letter_pdf) if generation else "",
            "letter_txt": path(generation.letter_path) if generation else "",
            "cv_docx": path(generation.cv_docx) if generation else "",
            "letter_state": (generation.state if generation else "") or "",
            "folder": path(folder),
            "status": item.get("status") or "",
            "application_id": item["application_id"],
            "job_id": item.get("job_id") or "",
        })
    return rows


def batch_csv(conn: sqlite3.Connection, batch_id: int) -> str:
    return _csv(batch_rows(conn, batch_id), BATCH_COLUMNS)


def write_batch_csv(conn: sqlite3.Connection, batch_id: int,
                    directory: str = "output/exports") -> Any:
    """The batch's CSV on disk, UTF-8 with a BOM so Excel and Numbers read the
    accents in company names correctly. Records the path on the batch."""
    import pathlib

    from ..apply.packager import slug
    from .tracker import set_batch_csv

    batch = conn.execute("SELECT name FROM export_batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError(f"no export batch {batch_id}")
    folder = pathlib.Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = (folder / f"{slug(str(batch['name']), limit=60)}.csv").resolve()
    target.write_text(batch_csv(conn, batch_id), encoding="utf-8-sig")
    set_batch_csv(conn, batch_id, str(target))
    return target
