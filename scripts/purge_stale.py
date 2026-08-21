#!/usr/bin/env python
"""Retention policy — §1.3 rule 3 and Phase 9 item 4 of PITWALL_PLAN.md.

Deletes candidates that have been `ignored` or untouched for N days (default
180, from `retention.purge_ignored_after_days`). Purpose limitation and
retention are architectural, not optional: this script is what makes the
"personal networking research" justification hold up.

Blocklisted slugs are never resurrected — deleting an ignored person does not
remove them from the blocklist.
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
        repos.conn.executemany(
            "DELETE FROM candidates WHERE id = ?", [(r["id"],) for r in rows]
        )
        console.print(f"[green]deleted {len(rows)} candidates[/]")

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
