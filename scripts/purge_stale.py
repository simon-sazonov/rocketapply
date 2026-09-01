#!/usr/bin/env python
"""Retention policy — §1.3 rule 3 and Phase 9 item 4 of PITWALL_PLAN.md.

Deletes candidates that have been `ignored` or untouched for N days (default
180, from `retention.purge_ignored_after_days`). Purpose limitation and
retention are architectural, not optional: this script is what makes the
"personal networking research" justification hold up.

Blocklisted slugs are never resurrected — deleting an ignored person does not
remove them from the blocklist.

§6.1 of PITWALL_V3_EMAIL_ENRICHMENT.md extends the rule to contact data, and
gives it a *second, stricter* clock. A purged candidate's `contact_emails` go
with them, explicitly rather than by cascade; and an address that has sat
unused past the window is dropped even when the person themselves is kept,
unless the owner has actually acted on that person. An email address is the
most sensitive thing this database holds, and the case for keeping it is
"I am going to write to them" — which expires.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.db import open_repos, vacuum  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()

STALE_SQL = """
SELECT c.id, c.full_name, c.linkedin_slug, c.last_seen_at,
       COALESCE(m.status, 'new') AS status, m.updated_at
  FROM candidates c
  LEFT JOIN crm m ON m.candidate_id = c.id
 WHERE COALESCE(m.status, 'new') NOT IN ('saved','contacted','replied','meeting')
   AND COALESCE(m.note, '') = ''
   AND (
        (COALESCE(m.status,'new') = 'ignored'
         AND COALESCE(m.updated_at, c.last_seen_at) < datetime('now', ?))
     OR (COALESCE(m.status,'new') = 'new'
         AND c.last_seen_at < datetime('now', ?))
   )
 ORDER BY c.last_seen_at ASC
"""

#: Contact rows whose own clock has run out, on people the owner never acted on.
#: `ACTED_ON` mirrors the candidate rule above: your judgement beats the timer,
#: so an address on someone you saved or wrote to stays until you say otherwise.
ACTED_ON = ("saved", "contacted", "replied", "meeting")

STALE_CONTACTS_SQL = f"""
SELECT e.id, e.candidate_id, e.status, e.source, e.retrieved_at,
       c.full_name, COALESCE(m.status, 'new') AS crm_status
  FROM contact_emails e
  JOIN candidates c ON c.id = e.candidate_id
  LEFT JOIN crm m   ON m.candidate_id = e.candidate_id
 WHERE COALESCE(m.status, 'new') NOT IN ({",".join("?" * len(ACTED_ON))})
   AND COALESCE(e.retrieved_at, c.last_seen_at) < datetime('now', ?)
 ORDER BY e.retrieved_at ASC
"""


def stale_contacts(conn, modifier: str) -> list[dict]:
    """Contact rows past the retention window on people never acted on."""
    return [dict(r) for r in conn.execute(STALE_CONTACTS_SQL, (*ACTED_ON, modifier))]


@app.command()
def main(
    days: int | None = typer.Option(None, help="override the retention window"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="preview by default"),
    vacuum_db: bool = typer.Option(False, "--vacuum", help="VACUUM after deleting"),
    purge_cache: bool = typer.Option(False, "--purge-cache", help="also drop expired SERP cache files"),
) -> None:
    """Delete stale candidates. Saved/contacted people and anyone with a note
    are never purged — your own judgement always wins over the timer."""
    settings = get_settings()
    window = days if days is not None else settings.retention_days
    modifier = f"-{int(window)} days"

    repos = open_repos(settings)
    rows = [dict(r) for r in repos.conn.execute(STALE_SQL, (modifier, modifier))]

    table = Table(title=f"stale candidates (> {window} days)")
    table.add_column("id", justify="right")
    table.add_column("name")
    table.add_column("status")
    table.add_column("last seen")
    for row in rows[:30]:
        table.add_row(str(row["id"]), row["full_name"] or "—", row["status"], row["last_seen_at"] or "—")
    console.print(table)
    if len(rows) > 30:
        console.print(f"[dim]... and {len(rows) - 30} more[/]")

    if not rows:
        console.print("[green]nothing to purge[/]")
    elif dry_run:
        console.print(
            f"[yellow]dry run:[/] {len(rows)} candidates would be deleted. "
            "Re-run with [bold]--apply[/] to actually delete them."
        )
    else:
        ids = [(r["id"],) for r in rows]
        # Explicitly, not by cascade — §6.1 makes the erasure of contact data a
        # promise, and a promise that only holds while a connection pragma is
        # set is not one. See `CRM.delete_and_block` for the same reasoning.
        repos.conn.executemany(
            "DELETE FROM contact_emails WHERE candidate_id = ?", ids
        )
        repos.conn.executemany("DELETE FROM candidates WHERE id = ?", ids)
        console.print(f"[green]deleted {len(rows)} candidates[/] (and any addresses held for them)")

    # ── the second clock: addresses on people who survive the purge ──
    purged_ids = {r["id"] for r in rows} if not dry_run else set()
    contacts = [
        c for c in stale_contacts(repos.conn, modifier)
        if c["candidate_id"] not in purged_ids
    ]
    if contacts:
        ctable = Table(title=f"stale contact addresses (> {window} days, never acted on)")
        ctable.add_column("person")
        ctable.add_column("status")
        ctable.add_column("source")
        ctable.add_column("found")
        for row in contacts[:20]:
            # The address itself is deliberately not printed. This report gets
            # pasted into terminals and issues; the count is the useful part.
            ctable.add_row(
                row["full_name"] or "—", row["status"] or "—",
                row["source"] or "—", (row["retrieved_at"] or "—")[:10],
            )
        console.print(ctable)
        if len(contacts) > 20:
            console.print(f"[dim]... and {len(contacts) - 20} more[/]")
        if dry_run:
            console.print(
                f"[yellow]dry run:[/] {len(contacts)} address(es) would be deleted, "
                "keeping the people themselves."
            )
        else:
            repos.conn.executemany(
                "DELETE FROM contact_emails WHERE id = ?", [(c["id"],) for c in contacts]
            )
            console.print(f"[green]deleted {len(contacts)} stale address(es)[/]")
    else:
        console.print("[green]no stale contact addresses[/]")

    if purge_cache:
        from pitwall.search import SerpCache

        cache = SerpCache(settings=settings)
        removed = 0
        for path in cache.root.rglob("*.json"):
            import json

            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if cache.is_expired(data.get("fetched_at")):
                if not dry_run:
                    path.unlink()
                removed += 1
        console.print(
            f"{'would remove' if dry_run else 'removed'} {removed} expired SERP cache files"
        )

    if vacuum_db and not dry_run:
        vacuum(repos.conn)
        console.print("[green]vacuumed[/] the database")


if __name__ == "__main__":
    app()
