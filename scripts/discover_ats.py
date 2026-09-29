#!/usr/bin/env python
"""Find each company's ATS board — §2.4 of PITWALL_V2_IMPLEMENTATION.md.

    .venv/bin/python scripts/discover_ats.py             # probe every company
    .venv/bin/python scripts/discover_ats.py --companies "PREMA Racing,AF Corse"
    .venv/bin/python scripts/discover_ats.py --show      # what is already known
    .venv/bin/python scripts/discover_ats.py --verify    # re-check what is recorded
    .venv/bin/python scripts/discover_ats.py --clear "AF Corse"
    .venv/bin/python scripts/discover_ats.py --targets   # the job-search list instead
    .venv/bin/python scripts/discover_ats.py --targets --verify   # re-check what is recorded

`--targets` probes `config/target_companies.csv` (the job search's employers)
rather than the people-search list, and writes what it finds back into that
CSV — which is the source of truth for targets, so the result is visible and
editable on the 🧭 Job Setup page. A row whose ATS is already filled is skipped.

Both lists adopt a board only with evidence that it is the company's
(`verify_board`): its postings name the company, or — on Lever and Ashby, which
name nobody — its token is the company's whole name. Answering with postings is
not evidence. The first live run adopted `recruitee:google` (a sample board)
for Google DeepMind and `lever:aleph` (an ad-sales agency) for Aleph Alpha.
On the people-search list, only the team's own name counts for a board that
names nobody, never an alias: its aliases are short forms kept for recall.

Most teams run bespoke portals, so expect a low hit rate — that is the finding,
not a failure. **Run this once.** The result is kept in `companies.ats_kind` /
`companies.ats_token`. The database is the only store for the people-search
list, because `config/companies.csv` has no ATS columns. Nothing re-probes on a
normal run. The first time a run is about to change those columns, it backs
the database up to `data/backups/` with SQLite's online backup API. `--dry-run`
writes nothing, so it takes no backup either.

Tokens are guessed from the company name and its domain, cheapest first, and a
404 is the ordinary answer for a wrong guess. Every request goes through the
same robots-gated, rate-limited client the adapters use, so a full pass over 49
companies is polite by construction — and slow. That is the other reason it
runs once.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import Settings, get_settings  # noqa: E402
from pitwall.db import backup_database, open_repos  # noqa: E402
from pitwall.jobsearch.companies import verify_board  # noqa: E402
from pitwall.sources.jobs import (  # noqa: E402
    ATS_KINDS,
    ATSSource,
    NotFound,
    PoliteClient,
    SourceError,
)

console = Console()

#: Words that carry no signal in a board slug.
_NOISE = ("racing", "team", "motorsport", "motorsports", "gp", "f1", "ltd",
          "limited", "gmbh", "srl", "spa", "inc", "llc", "sa", "ag", "bv")

#: `fetch(kind, token)` -> that board's postings. Raises SourceError / ValueError.
Fetch = Callable[[str, str], list[Any]]

#: Can an alias vouch for a board that names nobody, on the people-search
#: list? No: its aliases are short forms kept for recall. See `verify_board`.
PEOPLE_ALIAS_TOKENS = False


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


def board_fetcher(settings: Settings) -> Fetch:
    """`fetch(kind, token)` over one polite client, shared by every guess."""
    client = PoliteClient(settings)

    def fetch(kind: str, token: str) -> list[Any]:
        return ATSSource(kind, token, settings=settings, client=client).fetch()

    return fetch


def find_board(
    name: str,
    aliases: Iterable[str],
    tokens: list[str],
    kinds: list[str],
    fetch: Fetch,
    on_try: Callable[[str, str], None] | None = None,
    *,
    alias_tokens: bool = True,
) -> tuple[tuple[str, str, int] | None, list[tuple[str, str]]]:
    """The first candidate board `verify_board` accepts, and why others were not.

    Returns `((kind, token, n_postings) or None, [(board, reason), …])`. A guess
    that does not answer (a 404 is the normal answer for a wrong one) or answers
    with an empty board is not listed as rejected: there was nothing to judge.
    `alias_tokens` is `verify_board`'s.
    """
    aliases = list(aliases)
    rejected: list[tuple[str, str]] = []
    for token in tokens:
        for kind in kinds:
            if on_try:
                on_try(kind, token)
            try:
                postings = fetch(kind, token)
            except (SourceError, ValueError):
                continue
            ok, why = verify_board(name, aliases, token, postings, alias_tokens=alias_tokens)
            if ok:
                return (kind, token, len(postings)), rejected
            if postings:
                rejected.append((f"{kind}:{token}", why))
    return None, rejected


def recheck_board(name: str, aliases: Iterable[str], kind: str, token: str,
                  fetch: Fetch, *, alias_tokens: bool = True) -> tuple[str, str, int]:
    """Verdict on a board already recorded: `(verdict, reason, n_postings)`.

    `verdict` is "keep", "clear" or "unchecked". A board is cleared only on
    evidence that it is wrong: it is gone (404), its ATS is not one this
    project reads, or its postings fail `verify_board`. A timeout, a refusal or
    an empty board says nothing either way, so those are kept and reported.
    """
    try:
        postings = fetch(kind, token)
    except NotFound:
        return "clear", "the board is gone (404)", 0
    except SourceError as exc:
        return "unchecked", f"could not read it, kept: {str(exc)[:80]}", 0
    except ValueError as exc:
        return "clear", str(exc)[:80], 0
    if not postings:
        return "unchecked", "no postings to check it against, kept", 0
    ok, why = verify_board(name, list(aliases), token, postings, alias_tokens=alias_tokens)
    return ("keep" if ok else "clear"), why, len(postings)


def _show_guess(progress: Progress, task: TaskID, name: str) -> Callable[[str, str], None]:
    """`find_board`'s `on_try`: show the guess being made on the spinner line."""
    def on_try(kind: str, token: str) -> None:
        progress.update(task, description=f"{name} → {kind}:{token}")
    return on_try


class BoardStore:
    """Where the people-search list keeps its boards: `companies.ats_kind/ats_token`.

    The database is the only copy of them, so the first time a run is about to
    change one, the whole database is backed up (SQLite's online backup API)
    to `backup_dir`. A run that changes nothing leaves no backup behind, and a
    dry run changes nothing.
    """

    def __init__(self, conn: sqlite3.Connection, backup_dir: Path, *,
                 dry_run: bool = False) -> None:
        self.conn = conn
        self.backup_dir = backup_dir
        self.dry_run = dry_run
        self.backup: Path | None = None

    def record(self, company_id: int, kind: str, token: str) -> None:
        self._write("UPDATE companies SET ats_kind = ?, ats_token = ? WHERE id = ?",
                    (kind, token, company_id))

    def clear(self, company_id: int) -> None:
        self._write("UPDATE companies SET ats_kind = NULL, ats_token = NULL WHERE id = ?",
                    (company_id,))

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        if self.dry_run:
            return
        if self.backup is None:
            self.backup = backup_database(self.conn, self.backup_dir, label="pre-ats")
            console.print(f"[dim]backed up the database to {self.backup}[/]")
        self.conn.execute(sql, params)


# ───────────────────────────── the people search ─────────────────────────


def discover_companies(store: BoardStore, rows: list[dict[str, Any]], kinds: list[str],
                       limit_tokens: int, fetch: Fetch) -> list[tuple[str, str, str, int]]:
    """Probe each company row for a board and record the verified ones."""
    hits: list[tuple[str, str, str, int]] = []
    rejected: list[tuple[str, str, str]] = []
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("…", total=None)
        for row in rows:
            name = row["name"]
            tokens = candidate_tokens(name, row.get("domain"), row.get("aliases"))[:limit_tokens]
            hit, turned_down = find_board(
                name, row.get("aliases") or [], tokens, kinds, fetch,
                on_try=_show_guess(progress, task, name), alias_tokens=PEOPLE_ALIAS_TOKENS,
            )
            rejected.extend((name, board, why) for board, why in turned_down)
            if hit:
                kind, token, n = hit
                hits.append((name, kind, token, n))
                store.record(int(row["id"]), kind, token)

    table = Table(title=f"{len(hits)} board(s) found across {len(rows)} companies")
    table.add_column("company")
    table.add_column("ats")
    table.add_column("token")
    table.add_column("postings", justify="right")
    for name, kind, token, n in hits:
        table.add_row(name, kind, token, str(n))
    console.print(table)
    for name, board, why in rejected:
        console.print(f"[dim]rejected {board} for {name}: {why}[/]")
    if store.dry_run:
        console.print("[yellow]--dry-run: nothing was written[/]")
    elif hits:
        console.print(
            "Recorded on the company rows (companies.ats_kind / ats_token). "
            "`--verify` re-checks them; `--clear NAME` forgets one."
        )
    if not hits:
        console.print(
            "[yellow]No public ATS board matched.[/] That is the expected result for "
            "most motorsport teams. Add a careers_url to companies.csv and enable the "
            "`career_page` kind instead, or point config/job_sources.yaml at an "
            "aggregator."
        )
    return hits


def verify_companies(store: BoardStore, rows: list[dict[str, Any]],
                     fetch: Fetch) -> list[str]:
    """Re-check every recorded board on the rows; clear the ones shown to be wrong."""
    recorded = [r for r in rows if r.get("ats_kind") and r.get("ats_token")]
    if not recorded:
        console.print("[green]No company has an ATS board recorded — nothing to check.[/]")
        return []
    table = Table(title=f"{len(recorded)} recorded board(s), re-checked")
    for column in ("company", "board", "postings", "verdict"):
        table.add_column(column)
    colour = {"keep": "[green]✓ ", "clear": "[red]✕ ", "unchecked": "[yellow]? "}
    cleared: list[str] = []
    for row in recorded:
        verdict, why, n = recheck_board(row["name"], row.get("aliases") or [],
                                        row["ats_kind"], row["ats_token"], fetch,
                                        alias_tokens=PEOPLE_ALIAS_TOKENS)
        table.add_row(row["name"], f"{row['ats_kind']}:{row['ats_token']}", str(n),
                      colour[verdict] + why + "[/]")
        if verdict == "clear":
            store.clear(int(row["id"]))
            cleared.append(row["name"])
    console.print(table)
    if not cleared:
        console.print("[green]Every recorded board passed or could not be checked; "
                      "nothing was cleared.[/]")
    elif store.dry_run:
        console.print(f"[yellow]--dry-run: {len(cleared)} board(s) would be cleared[/]")
    else:
        console.print(f"[yellow]cleared {len(cleared)} board(s) that failed the check[/]")
    return cleared


# ───────────────────────────── the job search ────────────────────────────


def discover_targets(names: set[str] | None, kinds: list[str], limit_tokens: int,
                     dry_run: bool) -> None:
    """The same probe over config/target_companies.csv, written back to it."""
    from urllib.parse import urlparse

    from pitwall.jobsearch.companies import load_targets, save_targets, sync_targets

    settings = get_settings()
    targets, report = load_targets()
    for issue in report.errors:
        console.print(f"[red]target_companies.csv row {issue.row}:[/] {issue.message}")
    todo = [t for t in targets if t.active and not t.ats_kind
            and (names is None or t.name.casefold() in names)]
    if not todo:
        console.print("[green]Every selected target company already has an ATS recorded.[/]")
        return
    fetch = board_fetcher(settings)
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
            hit, turned_down = find_board(
                target.name, target.aliases, tokens, kinds, fetch,
                on_try=_show_guess(progress, task, target.name),
            )
            rejected.extend((target.name, board, why) for board, why in turned_down)
            if hit:
                kind, token, n = hit
                hits.append((target.name, kind, token, n))
                target.ats_kind, target.ats_token = kind, token
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
    from pitwall.jobsearch.companies import load_targets, save_targets, sync_targets

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
            ok, why = verify_board(target.name, target.aliases, target.ats_token, postings)
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
        False, "--verify", help="re-check the boards already recorded and clear wrong ones"
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
    store = BoardStore(repos.conn, settings.db_file.parent / "backups", dry_run=dry_run)

    if clear:
        wanted = {c.strip().casefold() for c in clear.split(",")}
        n = 0
        for row in rows:
            if row["name"].casefold() in wanted and row.get("ats_kind"):
                store.clear(int(row["id"]))
                n += 1
        console.print(f"[green]cleared {n} company/companies[/]"
                      + (" (--dry-run: nothing was written)" if dry_run else ""))
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

    if verify:
        verify_companies(store, rows, board_fetcher(settings))
        return

    rows = [r for r in rows if not r.get("ats_kind")]
    if not rows:
        console.print(
            "[green]Every selected company already has an ATS recorded.[/] "
            "Use --verify to re-check them, or --clear to re-probe one."
        )
        return

    kinds = [k.strip() for k in ats.split(",")] if ats else list(ATS_KINDS)
    bad = [k for k in kinds if k not in ATS_KINDS]
    if bad:
        console.print(f"[red]unknown ATS {bad}[/]; known: {sorted(ATS_KINDS)}")
        raise typer.Exit(1)

    console.print(
        f"probing [cyan]{len(rows)}[/] companies × {len(kinds)} ATSes × up to "
        f"{limit_tokens} token guesses, at {settings.jobs_crawl_delay:g}s between requests.\n"
        "[dim]A 404 is the normal answer for a wrong guess. Most motorsport teams run "
        "bespoke portals, so a low hit rate is the finding, not a failure.[/]"
    )
    discover_companies(store, rows, kinds, limit_tokens, board_fetcher(settings))


if __name__ == "__main__":
    typer.run(main)
