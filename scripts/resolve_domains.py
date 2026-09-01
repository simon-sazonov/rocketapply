#!/usr/bin/env python
"""Fill `companies.domain` — the highest-value free improvement available.

    .venv/bin/python scripts/resolve_domains.py                  # review only
    .venv/bin/python scripts/resolve_domains.py --apply          # write it
    .venv/bin/python scripts/resolve_domains.py --companies "Alpine,Ferrari"
    .venv/bin/python scripts/resolve_domains.py --show           # what is known

A terminal front end for `pitwall.enrich.discover`. The scoring, the engine
probe and the writing all live in the package, because the Setup page drives
exactly the same code and a rule that differed between them would be a bug
nobody could see.

**This narrows the field. It does not decide.** It ranks candidates, says why
each won, flags the ones that look like a retailer rather than a racing team,
and prints the alternatives. Read the table before `--apply`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.db import open_repos  # noqa: E402
from pitwall.enrich.discover import (  # noqa: E402
    DEFAULT_ENGINES,
    NoUsableEngine,
    apply_domains,
    discover_domains,
)

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


@app.command()
def main(
    companies: str | None = typer.Option(None, help="comma-separated names; default all"),
    apply: bool = typer.Option(False, "--apply", help="write the domains it found"),
    show: bool = typer.Option(False, "--show", help="print what is already known and stop"),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="replace domains that are already filled in"
    ),
    no_mx: bool = typer.Option(False, "--no-mx", help="skip the MX check"),
    engines: str = typer.Option(
        DEFAULT_ENGINES, help="SearXNG engines to try; the healthy ones are used"
    ),
    qps: float = typer.Option(
        0.4, help="queries per second. Lower than the app default on purpose: "
        "this runs a lot of queries through one engine and the whole point is "
        "not to get that engine suspended halfway through",
    ),
    plain: bool = typer.Option(
        False, "--plain", help="tab-separated, untruncated — for reading and piping"
    ),
    cached: bool = typer.Option(
        False, "--cached",
        help="reuse cached SERPs. Off by default: a re-run is usually an attempt "
        "to get a better answer than the cached one",
    ),
) -> None:
    """Resolve every domain a company uses, and show the working."""
    settings = get_settings()
    repos = open_repos(settings)
    rows = repos.companies.all(active_only=False)

    if companies:
        wanted = {n.strip().casefold() for n in companies.split(",") if n.strip()}
        rows = [r for r in rows if r["name"].casefold() in wanted]
    if not rows:
        console.print("[red]no matching companies[/]")
        raise typer.Exit(1)

    if show:
        table = Table(title="known domains")
        table.add_column("company")
        table.add_column("primary")
        table.add_column("all known")
        for row in rows:
            known = repos.domains.for_company(int(row["id"]))
            table.add_row(
                row["name"],
                row.get("domain") or "[dim]—[/]",
                ", ".join(d["domain"] for d in known) or "[dim]—[/]",
            )
        console.print(table)
        console.print(f"{sum(1 for r in rows if r.get('domain'))} of {len(rows)} filled in")
        raise typer.Exit(0)

    todo = rows if overwrite else [r for r in rows if not (r.get("domain") or "").strip()]
    if not todo:
        console.print("[green]every company already has a domain[/] — "
                      "use --overwrite to redo them")
        raise typer.Exit(0)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"), TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("resolving", total=len(todo))

        def tick(done: int, total: int, result, message: str) -> None:
            if message:
                progress.update(task, description=message[:38])
            else:
                progress.update(task, completed=done)

        try:
            run = discover_domains(
                repos, todo, settings, engines=engines, qps=qps,
                check_mx=not no_mx, use_cache=cached, on_progress=tick,
            )
        except NoUsableEngine as exc:
            progress.stop()
            console.print(f"\n[red]{exc}[/]")
            raise typer.Exit(1) from exc

    for engine, why in run.rejected:
        console.print(f"[dim]·[/] {engine}: [yellow]{why}[/]")
    console.print(f"[green]used {', '.join(run.engines)}[/]\n")
    if run.aborted:
        console.print(
            f"[yellow]Stopped with {run.remaining} still to do.[/] The engines "
            "gave out partway — what resolved before that is below and is worth "
            "applying; re-run in 15–60 minutes for the rest.\n"
        )

    if plain:
        # Tab-separated and untruncated. A rich table wraps long domains into
        # `tridentmotorspor…`, which is unreadable for exactly the rows that
        # most need reading.
        for item in run.results:
            print("\t".join([
                item.name,
                ",".join(item.domains) or "-",
                item.chosen.why() if item.chosen else "; ".join(item.warnings),
                ",".join(a.domain for a in item.alternatives),
            ]))
    else:
        table = Table(title=f"domains for {len(run.results)} companies")
        table.add_column("company", overflow="fold")
        table.add_column("domain(s)", overflow="fold")
        table.add_column("why", overflow="fold")
        table.add_column("alternatives", overflow="fold")
        for item in run.results:
            style = "green" if item.chosen and not item.warnings else (
                "yellow" if item.chosen else "red"
            )
            table.add_row(
                item.name,
                f"[{style}]{'  +  '.join(item.domains) if item.chosen else '—'}[/]",
                item.chosen.why() if item.chosen else "; ".join(item.warnings),
                ", ".join(a.domain for a in item.alternatives) or "[dim]none[/]",
            )
        console.print(table)
        for item in run.results:
            for warning in item.warnings:
                if item.chosen:
                    console.print(f"[yellow]check[/] {item.name}: {warning}")

    console.print(
        f"\n[green]{len(run.resolved)}[/] of {len(run.results)} resolved · "
        f"{len(run.needs_a_look)} need a look"
    )
    multi = [r for r in run.resolved if r.siblings]
    if multi:
        console.print(
            f"[dim]{len(multi)} use more than one domain — a lookup will offer "
            "one address per organisation:[/]"
        )
        for item in multi:
            console.print(f"  [dim]· {item.name}: {', '.join(item.domains)}[/]")

    if not apply:
        console.print(
            "\n[yellow]Nothing written.[/] Read the table, then re-run with "
            "[bold]--apply[/]. Anything wrong is one edit in config/companies.csv."
        )
        raise typer.Exit(0)

    written, recorded = apply_domains(repos, run.resolved, overwrite=overwrite)
    console.print(f"[green]wrote {written} primary domain(s)[/] to config/companies.csv")
    console.print(f"[green]recorded {recorded} domain(s)[/] in `company_domains`")
    console.print("Run [bold]pitwall validate[/] to re-sync the CSV into the database.")


if __name__ == "__main__":
    app()
