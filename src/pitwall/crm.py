"""The CRM state machine — §11 and Phase 7 of PITWALL_PLAN.md.

::

    new ──⭐──► saved ──📩──► contacted ──►replied──► meeting ──► closed
     │                            │
     └──────────❌──────────► ignored (terminal, hidden by default)

Notes are **append-only** with timestamps, never overwritten — `crm.note`
stores a running log. `delete_and_block` is both your "not interested" button
and your GDPR erasure mechanism (§1.3 rule 4).
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from .db import CRMRepo, loads
from .models import CRM_STATUSES

#: Allowed transitions. Any status may go to `ignored` or `closed`.
TRANSITIONS: dict[str, set[str]] = {
    "new": {"saved", "ignored", "contacted", "closed"},
    "saved": {"contacted", "ignored", "closed", "new"},
    "contacted": {"replied", "ignored", "closed", "saved"},
    "replied": {"meeting", "closed", "ignored", "contacted"},
    "meeting": {"closed", "replied", "ignored"},
    "ignored": {"new", "saved"},
    "closed": {"new", "saved", "contacted"},
}

TERMINAL = {"ignored", "closed"}

STATUS_EMOJI = {
    "new": "🆕",
    "saved": "⭐",
    "ignored": "❌",
    "contacted": "📩",
    "replied": "💬",
    "meeting": "☕",
    "closed": "📦",
}


class InvalidTransition(ValueError):
    pass


def can_transition(current: str, target: str) -> bool:
    if target not in CRM_STATUSES:
        return False
    if current == target:
        return True
    return target in TRANSITIONS.get(current, set())


class CRM:
    """Every CRM mutation goes through here so the rules stay in one place."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = CRMRepo(conn)

    # ── reads ──

    def get(self, candidate_id: int) -> dict[str, Any]:
        return self.repo.ensure(candidate_id)

    def status(self, candidate_id: int) -> str:
        return str(self.get(candidate_id).get("status") or "new")

    # ── writes ──

    def set_status(self, candidate_id: int, target: str, *, force: bool = False) -> str:
        current = self.status(candidate_id)
        if not force and not can_transition(current, target):
            raise InvalidTransition(f"{current} -> {target} is not an allowed transition")
        fields: dict[str, Any] = {"status": target}
        if target == "contacted":
            fields["contacted_at"] = datetime.now().isoformat(timespec="seconds")
        self.repo.update(candidate_id, **fields)
        return target

    def add_note(self, candidate_id: int, text: str, *, when: datetime | None = None) -> str:
        """Append a timestamped line. Existing notes are never overwritten."""
        text = (text or "").strip()
        if not text:
            return self.get(candidate_id).get("note") or ""
        stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
        entry = f"[{stamp}] {text}"
        existing = (self.get(candidate_id).get("note") or "").rstrip()
        combined = f"{existing}\n{entry}" if existing else entry
        self.repo.update(candidate_id, note=combined)
        return combined

    def add_tags(self, candidate_id: int, tags: list[str] | str) -> list[str]:
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("|", ",").split(",")]
        current = loads(self.get(candidate_id).get("tags"))
        for t in tags:
            t = str(t).strip().lower()
            if t and t not in current:
                current.append(t)
        self.repo.update(candidate_id, tags=current)
        return current

    def remove_tag(self, candidate_id: int, tag: str) -> list[str]:
        current = [t for t in loads(self.get(candidate_id).get("tags")) if t != tag.strip().lower()]
        self.repo.update(candidate_id, tags=current)
        return current

    def set_followup(self, candidate_id: int, days: int | None = None, *, on: date | None = None) -> str | None:
        """Set a follow-up date, either N days out or on an explicit date."""
        if on is None and days is None:
            self.repo.update(candidate_id, followup_at=None)
            return None
        target = on or (date.today() + timedelta(days=int(days or 0)))
        iso = target.isoformat()
        self.repo.update(candidate_id, followup_at=iso)
        return iso

    def snooze(self, candidate_id: int, days: int = 7) -> str | None:
        """Push an existing follow-up out by N days (from today if unset)."""
        current = self.get(candidate_id).get("followup_at")
        base = date.today()
        if current:
            with contextlib.suppress(ValueError):
                base = max(base, date.fromisoformat(str(current)[:10]))
        return self.set_followup(candidate_id, on=base + timedelta(days=days))

    def mark_contacted(self, candidate_id: int, *, note: str | None = None, followup_days: int | None = None) -> None:
        self.set_status(candidate_id, "contacted", force=True)
        if note:
            self.add_note(candidate_id, note)
        if followup_days:
            self.set_followup(candidate_id, followup_days)

    def save(self, candidate_id: int) -> str:
        return self.set_status(candidate_id, "saved", force=True)

    def ignore(self, candidate_id: int) -> str:
        return self.set_status(candidate_id, "ignored", force=True)

    def delete_and_block(self, candidate_id: int, reason: str | None = None) -> str | None:
        """Remove the person and never re-ingest them (§1.3 rule 4)."""
        row = self.conn.execute(
            "SELECT linkedin_slug FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        slug = row["linkedin_slug"] if row else None
        if slug:
            self.repo.block(slug, reason or "deleted from the UI")
        self.conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        return slug

    # ── queries the UI needs ──

    def due_followups(self, *, on: date | None = None) -> list[dict[str, Any]]:
        cutoff = (on or date.today()).isoformat()
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT c.id, c.full_name, c.current_title, c.current_company,
                       c.profile_url, m.status, m.followup_at, m.note
                  FROM crm m JOIN candidates c ON c.id = m.candidate_id
                 WHERE m.followup_at IS NOT NULL AND m.followup_at <= ?
                   AND m.status NOT IN ('ignored','closed')
                 ORDER BY m.followup_at ASC
                """,
                (cutoff,),
            )
        ]

    def counts(self) -> dict[str, int]:
        """Status counts anchored on `candidates`, not on `crm`.

        A candidate with no CRM row yet is `new` — the same COALESCE the leads
        view uses — so the metrics on Home, Pipeline and Analytics always agree
        with the Leads table.
        """
        counts = dict.fromkeys(CRM_STATUSES, 0)
        for row in self.conn.execute(
            """
            SELECT COALESCE(m.status, 'new') AS status, COUNT(*) AS n
              FROM candidates c
              LEFT JOIN crm m ON m.candidate_id = c.id
             GROUP BY COALESCE(m.status, 'new')
            """
        ):
            counts[row["status"]] = int(row["n"])
        return counts

    def contacted_since(self, days: int = 7) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM crm WHERE contacted_at IS NOT NULL AND contacted_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["n"])

    def reply_rate(self) -> float:
        counts = self.counts()
        contacted = counts["contacted"] + counts["replied"] + counts["meeting"]
        replied = counts["replied"] + counts["meeting"]
        return (replied / contacted) if contacted else 0.0

    def bulk_set_status(self, changes: dict[int, str]) -> int:
        n = 0
        for cid, status in changes.items():
            try:
                self.set_status(int(cid), status, force=True)
                n += 1
            except (ValueError, sqlite3.Error):
                continue
        return n
