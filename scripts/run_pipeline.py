#!/usr/bin/env python
"""PitWall CLI — the run recipes of §14.1 of PITWALL_PLAN.md.

    python scripts/run_pipeline.py init-db
    python scripts/run_pipeline.py build-queries --dry-run
    python scripts/run_pipeline.py run --max-queries 100 --max-results 20
    python scripts/run_pipeline.py run --companies "AF Corse,PREMA Racing"
    python scripts/run_pipeline.py score --rescore-all --prompt-version v2
    python scripts/run_pipeline.py search --cache-only && \\
    python scripts/run_pipeline.py extract --reprocess
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.logging import RichHandler  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.db import init_schema, open_repos  # noqa: E402
from pitwall.export import (  # noqa: E402
    export_view,
    filter_frame,
    funnel_counts,
    leads_frame,
    sweep_report,
    zero_result_companies,
)
from pitwall.inputs import load_and_sync  # noqa: E402
from pitwall.logging_setup import setup_logging  # noqa: E402
from pitwall.pipeline import Pipeline, RunParams, RunStats  # noqa: E402
from pitwall.query_builder import build_all, load_templates  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
console = Console()


def _csv_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _bootstrap(verbose: bool = False, *, use_serper: bool = False):
    settings = get_settings()
    if use_serper:
        # Serper's 2,500 credits are one-time. `--use-serper` is the deliberate
        # "yes, spend into the reserve" switch — see SERPER_RESERVE_CREDITS.
        settings = settings.with_overrides(serper_reserve_credits=0)
    settings.ensure_dirs()
    setup_logging(settings, console_level=logging.DEBUG if verbose else logging.WARNING,
                  handler=RichHandler(console=console, show_path=False))
    return settings


# ─────────────────────────────── commands ────────────────────────────────


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite schema and sync the input CSVs into it."""
    settings = _bootstrap()
    repos = open_repos(settings)
    version = init_schema(repos.conn)
    companies, roles, report = load_and_sync(repos.conn)
    console.print(f"[green]schema v{version}[/] at {settings.db_file}")
    console.print(f"synced {len(companies)} companies, {len(roles)} roles")
    _print_report(report)


@app.command("build-queries")
def build_queries(
    dry_run: bool = typer.Option(False, "--dry-run", help="print the plan, write nothing"),
    max_queries: int | None = typer.Option(None, help="override the budget cap"),
    companies: str | None = typer.Option(None, help="comma-separated company names"),
    roles: str | None = typer.Option(None, help="comma-separated role titles"),
    templates: str | None = typer.Option(None, help="comma-separated template ids"),
    top: int = typer.Option(40, help="how many rows to print"),
) -> None:
    """Stage 1 — turn the CSVs into a prioritized, deduplicated query list."""
    settings = _bootstrap()
    pipe = Pipeline(settings)
    params = RunParams(
        companies=_csv_list(companies),
        roles=_csv_list(roles),
        template_ids=_csv_list(templates),
        max_queries=max_queries,
    )
    comps, rls, report = pipe.load_inputs(params)
    _print_report(report)
    if not comps or not rls:
        console.print("[red]nothing to do — no active companies or roles[/]")
        raise typer.Exit(1)

    provider = pipe.chain()[0]
    specs, summary = build_all(
        comps, rls, settings,
        provider=provider.name,
        supports_operators=provider.supports_operators(),
        template_ids=params.template_ids,
        max_queries=max_queries,
    )
    console.print(
        f"Planned [bold]{summary['planned']:,}[/] unique queries "
        f"(capped to [bold]{summary['capped_to']:,}[/]) for provider "
        f"[cyan]{provider.name}[/] "
        f"(operators={'yes' if provider.supports_operators() else 'no'})"
    )
    table = Table(title=f"top {min(top, len(specs))} queries by priority")
    table.add_column("#", justify="right", style="dim")
    table.add_column("prio", justify="right")
    table.add_column("template")
    table.add_column("query")
    for i, q in enumerate(specs[:top], start=1):
        table.add_row(str(i), f"{q.priority:.1f}", q.template_id, q.query_text)
    console.print(table)

    if not dry_run:
        inserted, existing = pipe.repos.queries.insert_many(specs)
        console.print(f"[green]inserted {inserted}[/] new queries ({existing} already known)")


@app.command()
def search(
    limit: int | None = typer.Option(None, help="max queries to execute this pass"),
    max_results: int | None = typer.Option(None, help="results per query"),
    no_cache: bool = typer.Option(False, "--no-cache", help="force a refetch"),
    cache_only: bool = typer.Option(False, "--cache-only", help="offline: cached results only"),
    use_serper: bool = typer.Option(
        False, "--use-serper", help="spend into the SERPER_RESERVE_CREDITS floor"
    ),
    concurrency: int | None = typer.Option(None, help="worker threads for this run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Stage 2 — execute pending queries against the provider chain."""
    settings = _bootstrap(verbose, use_serper=use_serper)
    pipe = Pipeline(settings, on_progress=_progress)
    params = RunParams(
        limit=limit, max_results=max_results, no_cache=no_cache, cache_only=cache_only,
        concurrency=concurrency,
    )
    stats = RunStats()
    # A CLI stage opens a `runs` row exactly like the Streamlit worker does, so
    # a CLI-driven session shows up in Analytics and the Home panel (§0.5).
    with pipe.tracked(params, stats, "search"):
        pipe.search(params, stats)
    console.print(
        f"[green]{stats.queries_executed}[/] queries ran "
        f"({stats.queries_cached} from cache, {stats.queries_failed} failed) -> "
        f"[green]{stats.results_fetched}[/] new SERP rows"
    )


@app.command()
def extract(
    reprocess: bool = typer.Option(False, "--reprocess", help="re-parse every stored SERP row"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Stage 3 — turn SERP rows into distinct people."""
    settings = _bootstrap(verbose)
    pipe = Pipeline(settings, on_progress=_progress)
    params = RunParams(reprocess=reprocess)
    stats = RunStats()
    with pipe.tracked(params, stats, "extract"):
        pipe.extract(params, stats)
    console.print(
        f"[green]{stats.rows_parsed}[/] profile rows -> "
        f"[green]{stats.candidates_touched}[/] people "
        f"({stats.candidates_new} new, {stats.blocked_skipped} blocklisted)"
    )


@app.command()
def score(
    heuristic_only: bool = typer.Option(False, "--heuristic-only", help="skip the LLM stage"),
    rescore_all: bool = typer.Option(False, "--rescore-all", help="re-score everything"),
    prompt_version: str | None = typer.Option(None, help="prompt version to write"),
    threshold: float | None = typer.Option(None, help="override the heuristic threshold"),
    limit: int | None = typer.Option(None, help="max candidates to send to the LLM"),
    triage_min: float | None = typer.Option(
        None, "--triage-min", help="only send candidates at or above this heuristic score"
    ),
    triage_top: int | None = typer.Option(
        None, "--triage-top", help="hard cap on LLM candidates this run, best first"
    ),
    concurrency: int | None = typer.Option(None, help="LLM worker threads for this run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Stages 4 and 5 — heuristic pre-score, then the LLM filter."""
    settings = _bootstrap(verbose)
    pipe = Pipeline(settings, on_progress=_progress)
    params = RunParams(
        rescore_all=rescore_all,
        prompt_version=prompt_version,
        heuristic_threshold=threshold,
        use_llm=not heuristic_only,
        limit=limit,
        triage_min_heuristic=triage_min,
        triage_top_n=triage_top,
        concurrency=concurrency,
    )
    stats = RunStats()
    with pipe.tracked(params, stats, "score"):
        pipe.score_heuristics(params, stats)
        console.print(
            f"heuristics: [green]{stats.heuristic_scored}[/] scored, "
            f"[green]{stats.heuristic_passed}[/] above threshold"
        )
        if not heuristic_only:
            pipe.score_llm(params, stats)
    if not heuristic_only:
        console.print(
            f"LLM: [green]{stats.llm_scored}[/] scored in {stats.llm_calls} calls "
            f"({stats.llm_needs_review} need review, {stats.llm_triaged} skipped by triage) · "
            f"{stats.llm_tokens_in:,} in / {stats.llm_tokens_out:,} out tokens · "
            f"[bold]${stats.cost_estimate:.4f}[/]"
        )
    for note in stats.notes:
        console.print(f"[yellow]note:[/] {note}")


@app.command()
def run(
    max_queries: int | None = typer.Option(None, help="budget cap for this run"),
    max_results: int | None = typer.Option(None, help="results per query"),
    companies: str | None = typer.Option(None, help="comma-separated company names"),
    roles: str | None = typer.Option(None, help="comma-separated role titles"),
    templates: str | None = typer.Option(None, help="comma-separated template ids"),
    threshold: float | None = typer.Option(None, help="heuristic threshold"),
    no_llm: bool = typer.Option(False, "--no-llm", help="skip the LLM stage"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    cache_only: bool = typer.Option(False, "--cache-only"),
    use_serper: bool = typer.Option(
        False, "--use-serper", help="spend into the SERPER_RESERVE_CREDITS floor"
    ),
    concurrency: int | None = typer.Option(None, help="worker threads for this run"),
    triage_min: float | None = typer.Option(
        None, "--triage-min", help="only send candidates at or above this heuristic score"
    ),
    triage_top: int | None = typer.Option(
        None, "--triage-top", help="hard cap on LLM candidates this run, best first"
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """All stages, end to end, resumable."""
    settings = _bootstrap(verbose, use_serper=use_serper)
    pipe = Pipeline(settings, on_progress=_progress)
    params = RunParams(
        companies=_csv_list(companies),
        roles=_csv_list(roles),
        template_ids=_csv_list(templates),
        max_queries=max_queries,
        max_results=max_results,
        heuristic_threshold=threshold,
        use_llm=not no_llm,
        no_cache=no_cache,
        cache_only=cache_only,
        concurrency=concurrency,
        triage_min_heuristic=triage_min,
        triage_top_n=triage_top,
    )
    stats = pipe.run(params)
    table = Table(title=f"run #{stats.run_id}")
    table.add_column("stage")
    table.add_column("value", justify="right")
    for key, value in stats.as_dict().items():
        if key in {"run_id", "notes"}:
            continue
        table.add_row(key.replace("_", " "), f"{value:,.4f}" if isinstance(value, float) else f"{value:,}")
    console.print(table)
    for note in stats.notes:
        console.print(f"[yellow]note:[/] {note}")


@app.command()
def preview(
    max_queries: int | None = typer.Option(None),
    companies: str | None = typer.Option(None),
    roles: str | None = typer.Option(None),
) -> None:
    """Cost/quota preview before you commit to a run (§13.2)."""
    settings = _bootstrap()
    pipe = Pipeline(settings)
    info = pipe.preview(
        RunParams(companies=_csv_list(companies), roles=_csv_list(roles), max_queries=max_queries)
    )
    table = Table(title="run preview")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for k, v in info.items():
        table.add_row(k.replace("_", " "), str(v))
    console.print(table)
    console.print(
        f"\n[bold]This run plans {info['queries_fresh']} fresh queries -> "
        f"~{info['est_serp_rows']} SERP rows -> ~{info['est_candidates']} candidates -> "
        f"~{info['est_after_heuristics']} after heuristics -> {info['est_llm_calls']} LLM calls "
        f"-> est. ${info['est_cost_usd']:.4f} / "
        f"{info['llm_used_today']} of {info['llm_daily_cap']} free daily requests.[/]"
    )


@app.command()
def stats() -> None:
    """Where the database stands right now."""
    settings = _bootstrap()
    repos = open_repos(settings)
    funnel = funnel_counts(repos.conn, settings)
    table = Table(title="funnel")
    table.add_column("stage")
    table.add_column("count", justify="right")
    for k, v in funnel.items():
        table.add_row(k.replace("_", " "), f"{v:,}")
    console.print(table)

    qs = repos.queries.counts_by_status()
    console.print("queries: " + ", ".join(f"{k}={v}" for k, v in sorted(qs.items())) or "none")
    crm = repos.crm.counts_by_status()
    console.print("crm: " + (", ".join(f"{k}={v}" for k, v in sorted(crm.items())) or "none"))

    zeros = zero_result_companies(repos.conn)
    if zeros:
        console.print(
            f"\n[yellow]{len(zeros)} companies returned zero rows across all their queries — "
            "almost always a missing or wrong alias:[/]"
        )
        for z in zeros[:15]:
            console.print(f"  · {z['name']} (tier {z['tier']}, {z['queries']} queries)")


@app.command()
def coverage() -> None:
    """What the sweep has actually covered — §1.5's "record the result".

    Coverage before precision: v1 had searched 7 of 49 companies, so the next
    volume of leads was in the 42 nobody had looked at yet, not in a better
    threshold. Run this before and after a sweep and keep both.
    """
    settings = _bootstrap()
    repos = open_repos(settings)
    report = sweep_report(repos.conn, settings)

    searched = report["companies_searched"]
    total = report["companies_active"]
    pct = (searched / total * 100) if total else 0.0
    table = Table(title=f"coverage — {searched} of {total} companies ({pct:.0f}%)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key in ("companies_searched", "companies_with_serp_rows", "companies_with_people",
                "queries_done", "queries_failed", "serp_rows", "candidates"):
        table.add_row(key.replace("_", " "), f"{report[key]:,}")
    console.print(table)

    fired = report["templates_fired"]
    all_templates = [t.id for t in load_templates()]
    console.print(
        f"\ntemplates fired: [green]{len(fired)}[/] of {len(all_templates)} — "
        + ", ".join(fired)
    )
    missing = [t for t in all_templates if t not in fired]
    if missing:
        console.print(f"[yellow]never fired:[/] {', '.join(missing)}")

    gaps = Table(title="extraction gaps (lower is better)")
    gaps.add_column("field")
    gaps.add_column("missing", justify="right")
    gaps.add_column("of candidates", justify="right")
    n = max(1, report["candidates"])
    for key, label in (
        ("no_company", "current company"), ("no_title", "current title"),
        ("no_location", "location"), ("single_source", "only one source"),
    ):
        value = report["extraction"][key]
        gaps.add_row(label, f"{value:,}", f"{value / n * 100:.0f}%")
    console.print(gaps)

    console.print(
        "verdicts: "
        + ", ".join(f"{k}={v:,}" for k, v in report["verdicts"].items())
    )


@app.command()
def export(
    fmt: str = typer.Option("csv", "--format", help="csv | xlsx | md"),
    min_score: float = typer.Option(0.0, help="minimum final score"),
    status: str | None = typer.Option(None, help="comma-separated CRM statuses"),
    hide_ignored: bool = typer.Option(True, help="drop ignored people"),
    out: Path | None = typer.Option(None, help="explicit output path"),
) -> None:
    """Export the current filtered view (§8)."""
    settings = _bootstrap()
    repos = open_repos(settings)
    df = leads_frame(repos.conn, settings)
    df = filter_frame(
        df, score_range=(min_score, 100.0), statuses=_csv_list(status), hide_ignored=hide_ignored
    )
    if df.empty:
        console.print("[yellow]nothing matches those filters[/]")
        raise typer.Exit(1)
    path = export_view(df, fmt, out, settings)
    console.print(f"[green]exported {len(df)} rows[/] -> {path}")


@app.command("reset-db")
def reset_db(
    confirm: str = typer.Option(..., help="type the database filename to confirm"),
) -> None:
    """Delete the database file. Your CRM notes are the irreplaceable part (§14.3)."""
    settings = _bootstrap()
    if confirm != settings.db_file.name:
        console.print(f"[red]confirmation must be exactly {settings.db_file.name!r}[/]")
        raise typer.Exit(1)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(settings.db_file) + suffix)
        if p.exists():
            p.unlink()
    console.print(f"[green]deleted[/] {settings.db_file}")


def _progress(stage: str, message: str, current: int, total: int) -> None:
    bar = f" [{current}/{total}]" if total else ""
    console.print(f"[dim]{stage:>10}[/] {message}{bar}")


def _print_report(report) -> None:
    if not report.issues:
        return
    table = Table(title="input validation")
    table.add_column("file")
    table.add_column("row", justify="right")
    table.add_column("level")
    table.add_column("message")
    for issue in report.issues[:40]:
        style = "red" if issue.level == "error" else "yellow"
        table.add_row(issue.file, str(issue.row), f"[{style}]{issue.level}[/]", issue.message)
    console.print(table)
    if len(report.issues) > 40:
        console.print(f"[dim]... and {len(report.issues) - 40} more[/]")


if __name__ == "__main__":
    app()
