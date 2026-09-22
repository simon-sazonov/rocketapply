#!/usr/bin/env python
"""Find each company's ATS board — §2.4 of PITWALL_V2_IMPLEMENTATION.md.

    .venv/bin/python scripts/discover_ats.py             # probe every company
    .venv/bin/python scripts/discover_ats.py --companies "PREMA Racing,AF Corse"
    .venv/bin/python scripts/discover_ats.py --show      # what is already known
    .venv/bin/python scripts/discover_ats.py --clear "AF Corse"
    .venv/bin/python scripts/discover_ats.py --targets   # the job-search list instead
    .venv/bin/python scripts/discover_ats.py --targets --verify   # re-check what is recorded

`--targets` probes `config/target_companies.csv` (the job search's employers)
rather than the people-search list, and writes what it finds back into that
CSV — which is the source of truth for targets, so the result is visible and
editable on the 🧭 Job Setup page. A row whose ATS is already filled is skipped.

Most teams run bespoke portals, so expect a low hit rate — that is the finding,
not a failure. **Run this once.** The result is cached in
`companies.ats_kind` / `companies.ats_token`, `config/companies.csv` overrides
it by hand, and nothing re-probes on a normal run.

Tokens are guessed from the company name and its domain, cheapest first, and a
404 is the ordinary answer for a wrong guess. Every request goes through the
same robots-gated, rate-limited client the adapters use, so a full pass over 49
companies is polite by construction — and slow. That is the other reason it
runs once.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.progress import Progress, SpinnerColumn, TextColumn  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.db import open_repos  # noqa: E402
from pitwall.sources.jobs import ATS_KINDS, ATSSource, PoliteClient  # noqa: E402

console = Console()

#: Words that carry no signal in a board slug.
_NOISE = ("racing", "team", "motorsport", "motorsports", "gp", "f1", "ltd",
          "limited", "gmbh", "srl", "spa", "inc", "llc", "sa", "ag", "bv")


def candidate_tokens(name: str, domain: str | None = None, aliases: list[str] | None = None) -> list[str]:
    """Plausible board slugs for a company, most likely first.

    Greenhouse and Lever slugs are almost always the brand in lower case with
    the punctuation gone; the domain's second-level label is the next best guess
    because it is what the company actually calls itself.
    """
    out: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        slug = re.sub(r"[^a-z0-9]+", "", value.lower())
        if slug and slug not in out:
            out.append(slug)

    add(name)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name.lower()) if w]
    add("".join(words))
    stripped = [w for w in words if w not in _NOISE]
    if stripped and stripped != words:
        add("".join(stripped))
    if stripped:
        add(stripped[0])
    if domain:
        label = domain.lower().replace("https://", "").replace("http://", "")
        label = label.split("/")[0].removeprefix("www.").split(".")[0]
        add(label)
    for alias in aliases or []:
        add(alias)
    return out[:6]


def discover_targets(names: set[str] | None, kinds: list[str], limit_tokens: int,
                     dry_run: bool) -> None:
    """The same probe over config/target_companies.csv, written back to it."""
    from urllib.parse import urlparse

    from pitwall.jobsearch.companies import (
        load_targets,
        save_targets,
        sync_targets,
        verify_board,
    )
    from pitwall.sources.jobs import SourceError

    settings = get_settings()
    targets, report = load_targets()
    for issue in report.errors:
        console.print(f"[red]target_companies.csv row {issue.row}:[/] {issue.message}")
    todo = [t for t in targets if t.active and not t.ats_kind
            and (names is None or t.name.casefold() in names)]
    if not todo:
        console.print("[green]Every selected target company already has an ATS recorded.[/]")
        return
    client = PoliteClient(settings)
    console.print(
        f"probing [cyan]{len(todo)}[/] target companies × {len(kinds)} ATSes × up to "
        f"{limit_tokens} token guesses. A 404 is the normal answer for a wrong guess."
    )
    hits: list[tuple[str, str, str, int]] = []
    rejected: list[tuple[str, str, str]] = []
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("…", total=None)
        for target in todo:
            domain = urlparse(target.careers_url).netloc if target.careers_url else None
            tokens = candidate_tokens(target.name, domain, target.aliases)[:limit_tokens]
            for token in tokens:
                found = False
                for kind in kinds:
                    progress.update(task, description=f"{target.name} → {kind}:{token}")
                    try:
                        postings = ATSSource(kind, token, settings=settings,
                                             client=client).fetch()
                    except (SourceError, ValueError):
                        continue
                    ok, why = verify_board(target, token, postings)
                    if not ok:
                        if postings:
                            rejected.append((target.name, f"{kind}:{token}", why))
                        continue
                    hits.append((target.name, kind, token, len(postings)))
                    target.ats_kind, target.ats_token = kind, token
                    found = True
                    break
                if found:
                    break
    table = Table(title=f"{len(hits)} board(s) found across {len(todo)} target companies")
    for column in ("company", "ats", "token", "postings"):
        table.add_column(column)
    for name, kind, token, n in hits:
        table.add_row(name, kind, token, str(n))
    console.print(table)
    for name, board, why in rejected:
        console.print(f"[dim]rejected {board} for {name}: {why}[/]")
    if dry_run:
        console.print("[yellow]--dry-run: nothing was written[/]")
    elif hits:
        save_targets(targets)
        repos = open_repos(settings)
        sync_targets(repos.conn, targets)
        console.print("Written to [bold]config/target_companies.csv[/] (ats_kind, ats_token).")
    missing = [t.name for t in todo if not t.ats_kind]
    if missing:
        console.print(
            f"[yellow]{len(missing)} without a public board[/] — most large employers run "
            "Workday or their own portal. Their careers_url is shown as a link on the "
            "🧭 Job Setup page: " + ", ".join(missing[:12]) + ("…" if len(missing) > 12 else "")
        )


def verify_targets(dry_run: bool) -> None:
    """Re-check every recorded target board, and clear the ones that fail."""
    from pitwall.jobsearch.companies import (
        load_targets,
        save_targets,
        sync_targets,
        verify_board,
    )
    from pitwall.sources.jobs import SourceError

    settings = get_settings()
    targets, _ = load_targets()
    client = PoliteClient(settings)
    table = Table(title="recorded target boards, re-checked")
    for column in ("company", "board", "postings", "verdict"):
        table.add_column(column)
    cleared = 0
    for target in targets:
        if not target.ats_kind or not target.ats_token:
            continue
        try:
            postings = ATSSource(target.ats_kind, target.ats_token, settings=settings,
                                 client=client).fetch()
        except (SourceError, ValueError) as exc:
            postings, why, ok = [], str(exc)[:60], False
        else:
            ok, why = verify_board(target, target.ats_token, postings)
        table.add_row(target.name, f"{target.ats_kind}:{target.ats_token}", str(len(postings)),
                      ("[green]✓ " if ok else "[red]✕ ") + why + "[/]")
        if not ok:
            target.ats_kind = target.ats_token = None
            cleared += 1
    console.print(table)
    if cleared and not dry_run:
        save_targets(targets)
        sync_targets(open_repos(settings).conn, targets)
        console.print(f"[yellow]cleared {cleared} board(s) that failed the check[/]")


def main(
    companies: str | None = typer.Option(None, help="comma-separated names; default = all active"),
    limit_tokens: int = typer.Option(4, help="how many slug guesses per company"),
    ats: str | None = typer.Option(None, help="comma-separated ATSes to probe"),
    show: bool = typer.Option(False, "--show", help="print what is already known and stop"),
    clear: str | None = typer.Option(None, help="forget the ATS for these companies"),
    dry_run: bool = typer.Option(False, "--dry-run", help="probe but write nothing"),
    targets: bool = typer.Option(
        False, "--targets", help="probe config/target_companies.csv (the job search) instead"
    ),
    verify: bool = typer.Option(
        False, "--verify", help="with --targets: re-check recorded boards, clear wrong ones"
    ),
) -> None:
    if targets and verify:
        verify_targets(dry_run)
        return
    if targets:
        kinds = [k.strip() for k in ats.split(",")] if ats else list(ATS_KINDS)
        wanted = {c.strip().casefold() for c in companies.split(",")} if companies else None
        discover_targets(wanted, kinds, limit_tokens, dry_run)
        return
    settings = get_settings()
    repos = open_repos(settings)
    rows = repos.companies.all(active_only=True)

    if clear:
        wanted = {c.strip().casefold() for c in clear.split(",")}
        n = 0
        for row in rows:
            if row["name"].casefold() in wanted:
                repos.conn.execute(
                    "UPDATE companies SET ats_kind = NULL, ats_token = NULL WHERE id = ?",
                    (row["id"],),
                )
                n += 1
        console.print(f"[green]cleared {n} company/companies[/]")
        return

    if show:
        known = [r for r in rows if r.get("ats_kind")]
        table = Table(title=f"{len(known)} of {len(rows)} companies have a known ATS board")
        table.add_column("company")
        table.add_column("ats")
        table.add_column("token")
        for row in known:
            table.add_row(row["name"], row["ats_kind"], row["ats_token"])
        console.print(table)
        if not known:
            console.print("[dim]Nothing discovered yet. Run without --show to probe.[/]")
        return

    if companies:
        wanted = {c.strip().casefold() for c in companies.split(",")}
        rows = [r for r in rows if r["name"].casefold() in wanted]
    rows = [r for r in rows if not r.get("ats_kind")]
    if not rows:
        console.print(
            "[green]Every selected company already has an ATS recorded.[/] "
            "Use --clear to re-probe one."
        )
        return

    kinds = [k.strip() for k in ats.split(",")] if ats else list(ATS_KINDS)
    bad = [k for k in kinds if k not in ATS_KINDS]
    if bad:
        console.print(f"[red]unknown ATS {bad}[/]; known: {sorted(ATS_KINDS)}")
        raise typer.Exit(1)

    client = PoliteClient(settings)
    console.print(
        f"probing [cyan]{len(rows)}[/] companies × {len(kinds)} ATSes × up to "
        f"{limit_tokens} token guesses, at {settings.jobs_crawl_delay:g}s between requests.\n"
        "[dim]A 404 is the normal answer for a wrong guess. Most motorsport teams run "
        "bespoke portals, so a low hit rate is the finding, not a failure.[/]"
    )

    hits: list[tuple[str, str, str, int]] = []
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("…", total=None)
        for row in rows:
            tokens = candidate_tokens(row["name"], row.get("domain"), row.get("aliases"))[:limit_tokens]
            found = False
            for token in tokens:
                for kind in kinds:
                    progress.update(task, description=f"{row['name']} → {kind}:{token}")
                    n = ATSSource(kind, token, settings=settings, client=client).probe()
                    if n:
                        hits.append((row["name"], kind, token, n))
                        if not dry_run:
                            repos.conn.execute(
                                "UPDATE companies SET ats_kind = ?, ats_token = ? WHERE id = ?",
                                (kind, token, row["id"]),
                            )
                        found = True
                        break
                if found:
                    break

    table = Table(title=f"{len(hits)} board(s) found across {len(rows)} companies")
    table.add_column("company")
    table.add_column("ats")
    table.add_column("token")
    table.add_column("postings", justify="right")
    for name, kind, token, n in hits:
        table.add_row(name, kind, token, str(n))
    console.print(table)
    if dry_run:
        console.print("[yellow]--dry-run: nothing was written[/]")
    elif hits:
        console.print(
            "Recorded on the company rows. They are also columns in "
            "[bold]config/companies.csv[/] — edit there to override, and this script "
            "will leave them alone."
        )
    if not hits:
        console.print(
            "[yellow]No public ATS board matched.[/] That is the expected result for "
            "most motorsport teams. Add a careers_url to companies.csv and enable the "
            "`career_page` kind instead, or point config/job_sources.yaml at an "
            "aggregator."
        )


if __name__ == "__main__":
    typer.run(main)
