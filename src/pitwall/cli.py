"""The PitWall command line — §14.1 of PITWALL_PLAN.md, §3.2/§3.3 of the v2 brief.

Installed as `pitwall`; `python scripts/run_pipeline.py` is the same app for
anyone who has not run `pip install -e .`.

    pitwall init                       # first run: config, .env, providers
    pitwall validate                   # what is wrong with your CSVs, and why
    pitwall run --max-queries 100
    pitwall run --companies "AF Corse,PREMA Racing"
    pitwall score --rescore-all --prompt-version v2
    pitwall jobs --score-fit           # live openings, and how they fit you
    pitwall openings                   # who to write to, and why now
    pitwall enrich -c 41 -c 88         # find addresses, free engine only
    pitwall search --cache-only && pitwall extract --reprocess
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import get_settings
from .db import init_schema, open_repos
from .export import (
    CONTACT_EXPORT_WARNING,
    evidence_text,
    export_view,
    filter_frame,
    funnel_counts,
    leads_frame,
    openings_frame,
    sweep_report,
    zero_result_companies,
)
from .inputs import load_and_sync
from .logging_setup import setup_logging
from .pipeline import Pipeline, RunParams, RunStats
from .query_builder import build_all, load_templates

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


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="overwrite config/*.csv and .env"),
    yes: bool = typer.Option(False, "--yes", "-y", help="accept every default, ask nothing"),
    skip_searxng: bool = typer.Option(False, "--skip-searxng"),
) -> None:
    """First run — config, .env, providers, and a validation report (§3.2).

    Safe to run twice: nothing that already exists is overwritten without
    `--force`, so an interrupted setup gets finished rather than reset.
    """
    from pitwall import setup_wizard as wiz

    console.print("[bold]PitWall setup[/]\n")
    result = wiz.InitResult()

    # ── 1. the interpreter, and the Anaconda trap by name ──
    step = wiz.check_python()
    result.steps.append(step)
    console.print(f"{'[green]✓[/]' if step.ok else '[red]✗[/]'} python — {step.detail}")
    if not step.ok:
        console.print(f"  [yellow]{step.action}[/]")
        raise typer.Exit(1)

    # ── 2. sample CSVs ──
    created, skipped = wiz.write_sample_inputs(force=force)
    result.created += created
    result.skipped += skipped
    for path in created:
        console.print(f"[green]✓[/] wrote {path.relative_to(wiz.project_root())}")
    for path in skipped:
        rows = wiz.count_rows(path)
        console.print(
            f"[dim]·[/] kept {path.relative_to(wiz.project_root())} ({rows} rows)"
        )
        # This repository ships the owner's own lists. A friend cloning it
        # inherits 49 motorsport teams they have never heard of, and would
        # otherwise have to work out why their first run searched for those.
        if rows > 5:
            console.print(
                "  [yellow]that is someone else's list if you have just cloned this[/] — "
                "delete the file and re-run, or `pitwall init --force`, to replace it "
                "with a commented sample"
            )
    result.add("inputs", True)

    # ── 3. .env ──
    env_created, env_skipped = wiz.write_env(force=force)
    if env_created:
        console.print("[green]✓[/] wrote .env from .env.example")
    elif env_skipped:
        console.print("[dim]·[/] kept .env (already there)")
    else:
        console.print("[yellow]![/] no .env.example to copy")
    result.add("env", True)

    # ── 4. the LLM path ──
    settings = get_settings()
    if settings.groq_api_key:
        console.print("[green]✓[/] GROQ_API_KEY is set — run `make groq-check` to verify it")
    else:
        console.print(
            "\n[bold]Which LLM?[/]\n"
            "  [cyan]1[/] Groq — free, no credit card, ~1s per batch. "
            "Key from https://console.groq.com  [green](recommended)[/]\n"
            "  [cyan]2[/] Ollama — local, free, no key, and minutes per batch on CPU\n"
            "  [cyan]3[/] neither for now — heuristics still work on their own"
        )
        choice = "1" if yes else typer.prompt("choose", default="1")
        if choice == "1":
            console.print(
                "  Put it in [bold].env[/] as `GROQ_API_KEY=gsk_…`, then:\n"
                "    make groq-check      # lists the models your key can reach"
            )
        elif choice == "2":
            console.print(
                "  Then:\n"
                "    python scripts/setup_ollama.py   # pulls and configures the model\n"
                "    LLM_PROVIDER_CHAIN=ollama        # in .env"
            )
        else:
            console.print("  Fine — run `pitwall score --heuristic-only` and add a key later.")
    result.add("llm", True)

    # ── 5. SearXNG ──
    if skip_searxng:
        console.print("[dim]·[/] skipped SearXNG")
    elif not wiz.docker_available():
        console.print(
            "[yellow]![/] Docker is not running, so SearXNG cannot start. That is "
            "survivable: the chain falls back to `ddgs`, but operator templates stay "
            "disabled and coverage suffers. See docker/searxng/README.md."
        )
    else:
        want = True if yes else typer.confirm(
            "Start the local SearXNG container? (unlocks operator queries)", default=True
        )
        if want:
            console.print("  starting… (the first run pulls ~1 GB)")
            ok, output = wiz.start_searxng()
            if ok:
                console.print("[green]✓[/] SearXNG is up — verify with `make searxng-check`")
            else:
                console.print(f"[yellow]![/] it did not come up:\n[dim]{output}[/]")
    result.add("searxng", True)

    # ── 6. validate ──
    console.print("\n[bold]Validating your inputs[/]")
    console.print(wiz.vocabulary_hint())
    report = wiz.validate_inputs()
    _print_report(report)
    console.print(
        f"{report.rows_ok} of {report.rows_read} rows loaded cleanly · "
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    )

    console.print(
        "\n[bold]Next[/]\n"
        "  1. edit config/companies.csv and config/roles.csv — aliases drive recall,\n"
        "     negative_keywords drive precision\n"
        "  2. pitwall validate            # check them\n"
        "  3. pitwall preview             # what a run would cost, before it runs\n"
        "  4. pitwall run --max-queries 100\n"
        "  5. streamlit run app/Home.py"
    )


@app.command()
def validate() -> None:
    """What is wrong with your CSVs, and why it matters (§3.3).

    The same validation the Setup page runs — surfaced here so it can be checked
    before a run rather than after one.
    """
    from pitwall import setup_wizard as wiz

    _bootstrap()
    console.print(wiz.vocabulary_hint() + "\n")
    report = wiz.validate_inputs()
    _print_report(report)

    if report.ok and not report.warnings:
        console.print(f"[green]all {report.rows_read} rows are clean[/]")
        return
    console.print(
        f"{report.rows_ok} of {report.rows_read} rows loaded · "
        f"[{'red' if report.errors else 'green'}]{len(report.errors)} error(s)[/] · "
        f"[yellow]{len(report.warnings)} warning(s)[/]"
    )
    if report.errors:
        console.print("[red]Rows with errors are skipped on load.[/]")
        raise typer.Exit(1)
    console.print(
        "[dim]Warnings are not fatal, but two of them cost you real leads: a company "
        "with no aliases is a recall problem, and one with no negative_keywords is a "
        "precision problem.[/]"
    )


README_BEGIN = "<!-- BEGIN generated: csv-columns -->"
README_END = "<!-- END generated: csv-columns -->"


@app.command()
def columns(
    check: bool = typer.Option(False, "--check", help="fail if the README is stale"),
    write: bool = typer.Option(False, "--write", help="regenerate the README block"),
) -> None:
    """The CSV column contract, generated from the validation rules (§3.3).

    The README's table is produced from the same `ColumnSpec` list the loader
    validates against, so the two cannot drift. `--check` is the guard; it is
    also a test.
    """
    from .config import project_root
    from .inputs import column_docs

    docs = column_docs()
    readme = project_root() / "README.md"

    if not (check or write):
        console.print(docs)
        return

    text = readme.read_text(encoding="utf-8") if readme.exists() else ""
    if README_BEGIN not in text or README_END not in text:
        console.print(
            f"[red]README.md has no generated block[/] — add these two markers "
            f"around the column tables:\n  {README_BEGIN}\n  {README_END}"
        )
        raise typer.Exit(1)

    head, rest = text.split(README_BEGIN, 1)
    _, tail = rest.split(README_END, 1)
    fresh = f"{head}{README_BEGIN}\n{docs}\n{README_END}{tail}"

    if write:
        readme.write_text(fresh, encoding="utf-8")
        console.print("[green]README.md column tables regenerated[/]")
        return
    if fresh != text:
        console.print(
            "[red]README.md is out of date[/] — the column rules changed. "
            "Run `pitwall columns --write`."
        )
        raise typer.Exit(1)
    console.print("[green]README.md matches the validation rules[/]")


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
def jobs(
    kinds: str | None = typer.Option(
        None, help="comma-separated: ats_json, aggregator, career_page"
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="force a refetch"),
    score_fit: bool = typer.Option(
        False, "--score-fit",
        help="also score each posting against config/profile.md (§2.6, optional)",
    ),
    limit: int | None = typer.Option(None, help="max postings to score for fit"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Stage 6 — refresh live job postings from every enabled source (§2)."""
    settings = _bootstrap(verbose)
    pipe = Pipeline(settings, on_progress=_progress)
    params = RunParams(job_kinds=_csv_list(kinds), no_cache=no_cache, limit=limit)
    stats = RunStats()
    with pipe.tracked(params, stats, "jobs"):
        pipe.poll_jobs(params, stats)
        if score_fit:
            pipe.score_jobs(params, stats)

    console.print(
        f"[green]{stats.jobs_seen}[/] posting(s) seen · "
        f"[green]{stats.jobs_new}[/] new · {stats.jobs_closed} closed · "
        f"{stats.job_sources_failed} source(s) failed"
        + (f" · {stats.jobs_scored} scored for fit" if score_fit else "")
    )
    counts = pipe.repos.jobs.counts_by_status()
    console.print("jobs: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    for note in stats.notes:
        console.print(f"[yellow]note:[/] {note}")


@app.command()
def openings(
    company: str | None = typer.Option(None, help="one company name"),
    top: int = typer.Option(20, help="how many openings to print"),
    min_score: float = typer.Option(0.0, help="minimum person score to list"),
) -> None:
    """The §2.5 payoff: open jobs, and who you already know there."""
    settings = _bootstrap()
    repos = open_repos(settings)
    rows = openings_frame(repos.conn, settings, company=company, min_score=min_score)
    if not rows:
        console.print(
            "[yellow]No open postings yet.[/] Run "
            "[bold]python scripts/run_pipeline.py jobs[/] first, and see "
            "config/job_sources.yaml."
        )
        raise typer.Exit(1)

    for row in rows[:top]:
        age = f"{row['age_days']}d ago" if row["age_days"] is not None else "age unknown"
        console.print(
            f"\n[bold]{row['title']}[/] — {row['company_name'] or 'unknown company'}"
            f" · {row['location'] or 'location unknown'} · {age}"
        )
        console.print(f"  [dim]{row['url']}[/]")
        if not row["people"]:
            console.print("  [dim]nobody in the database at this company yet[/]")
            continue
        total = len(row["people"])
        console.print(
            f"  [green]{total} {'person' if total == 1 else 'people'} you already have[/]"
            f" · {row['same_family_count']} in this discipline"
            + (f", {row['uncontacted_same_family']} of them not contacted"
               if row["uncontacted_same_family"] else "")
        )
        for person in row["people"][:5]:
            console.print(
                f"    · {person['name']} — {person['title'] or '—'} "
                f"[dim](score {person['score']:.0f} · {person['status']} · "
                f"{person['proximity']})[/]"
            )


@app.command()
def enrich(
    candidate_id: list[int] = typer.Option(
        ..., "--candidate-id", "-c",
        help="who to look up. Repeat the flag for several people.",
    ),
    use_hunter: bool = typer.Option(
        False, "--hunter",
        help="allow the paid Hunter escalation. Without it nothing can spend.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="run the free engine but write nothing"
    ),
    force: bool = typer.Option(
        False, "--force", help="re-harvest the company even if it was done recently"
    ),
    github: bool = typer.Option(
        False, "--github",
        help="also read public commit emails (§2.4). Free; best for software "
        "and simulation people, useless for anyone with no public code.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Find email addresses for named people — §7.3 of the v3 brief.

    **Without `--hunter` this cannot spend anything**, and that is structural
    rather than a promise: the chain is built from steps whose `spends_credits`
    is False, so there is nothing in it that could bill you.

    Never operates on the whole table. Enrichment is a manual tool on a manual
    selection, and the selection is these `--candidate-id` flags.
    """
    from . import enrich as enrichment

    settings = _bootstrap(verbose)
    repos = open_repos(settings)
    ids = list(dict.fromkeys(candidate_id))

    steps = enrichment.free_chain(force_harvest=force, with_github=github)
    if use_hunter:
        try:
            from .enrich.hunter import HunterFinderStep, hunter_available
        except ImportError:  # pragma: no cover - only before step 7 of §10
            console.print("[yellow]the Hunter adapter is not available[/]")
        else:
            ok, why = hunter_available(settings, repos)
            if not ok:
                console.print(f"[yellow]Hunter is unavailable:[/] {why}")
            else:
                steps = [*steps, HunterFinderStep()]

    pipe = Pipeline(settings, repos, on_progress=_progress)
    params = RunParams(limit=len(ids))
    stats = RunStats()
    results: list[dict] = []
    with pipe.tracked(params, stats, "enrich") as run_id:
        contexts = enrichment.enrich_many(
            ids, repos, settings, steps=steps, run_id=run_id,
            on_progress=lambda i, n, cid: pipe.progress(
                "enrich", f"person {i} of {n}", i, n
            ),
        )
        for ctx in contexts:
            if not dry_run:
                enrichment.persist_findings(ctx)
            results.append(enrichment.summary(ctx))

    if not results:
        console.print("[yellow]nothing to do — no such candidate[/]")
        raise typer.Exit(1)

    table = Table(title="contact enrichment")
    table.add_column("person")
    table.add_column("status")
    table.add_column("address")
    table.add_column("evidence")
    for row in results:
        colour = {
            "verified": "green", "probable": "yellow",
            "guess": "dim", "not_found": "red",
        }[row["status"]]
        table.add_row(
            row["name"] or "—",
            f"[{colour}]{row['status']}[/]",
            row["email"] or "—",
            # The evidence travels with the label, always — §4.
            evidence_text(row["evidence"]) or "—",
        )
    console.print(table)

    spent = sum(r["credits_spent"] for r in results)
    found = sum(1 for r in results if r["status"] != "not_found")
    console.print(
        f"{found} of {len(results)} resolved · "
        + (f"[yellow]{spent:g} Hunter credit(s) spent[/]" if spent
           else "[green]nothing spent[/]")
        + (" · [dim]dry run: nothing written[/]" if dry_run else "")
    )
    for row in results:
        for note in row["notes"]:
            console.print(f"[yellow]note ({row['name']}):[/] {note}")


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
    include_contacts: bool = typer.Option(
        False, "--include-contacts",
        help="also write email addresses. Off by default — see §6.1 of the v3 brief",
    ),
) -> None:
    """Export the current filtered view (§8).

    Email addresses are **excluded unless you ask for them**. `--include-contacts`
    is the deliberate switch, and it prints what you are agreeing to before it
    writes the file.
    """
    settings = _bootstrap()
    repos = open_repos(settings)
    df = leads_frame(repos.conn, settings)
    df = filter_frame(
        df, score_range=(min_score, 100.0), statuses=_csv_list(status), hide_ignored=hide_ignored
    )
    if df.empty:
        console.print("[yellow]nothing matches those filters[/]")
        raise typer.Exit(1)
    if include_contacts:
        with_email = int(df["contact_email"].notna().sum()) if "contact_email" in df else 0
        console.print(f"[yellow]⚠️  {CONTACT_EXPORT_WARNING}[/]")
        console.print(f"[yellow]   {with_email} of {len(df)} rows carry an address.[/]")
    path = export_view(df, fmt, out, settings, include_contacts=include_contacts)
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
