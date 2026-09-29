"""`pitwall apply` — the tailored CV and cover-letter commands.

    pitwall apply validate-profile     # is profile/ sound?
    pitwall apply calibrate            # does the frame still measure the way it claims?

Mounted on the main Typer app in `pitwall.cli`.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .layout import Frame
from .validate import format_report, validate

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
console = Console()


@app.command("validate-profile")
def validate_profile(
    strict: bool = typer.Option(False, "--strict", help="TODO_SIMON markers become errors"),
) -> None:
    """Frame compliance and data integrity for `profile/`.

    Run this in CI on every profile commit. Nothing downstream is trustworthy
    without it: an unknown tag matches nothing, which looks exactly like having
    no experience.
    """
    report = validate()
    print(format_report(report, strict=strict))
    raise typer.Exit(1 if report.failed(strict=strict) else 0)


@app.command()
def calibrate(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="list every project's line cost"),
) -> None:
    """Re-measure the frame with real font metrics and report any drift.

    Three questions, because there are three ways the page can quietly stop
    fitting: does the measurement agree with the line counts declared in
    `layout.yaml`, does it agree with the character wrap the validator uses, and
    how much slack is left in the geometry before some bullet changes line count.
    """
    from .calibrate import calibrate as run_calibration

    report = run_calibration()

    if not report.measured:
        console.print("[yellow]![/] fonts are not installed — this proves nothing about the "
                      "rendered page. Run [bold]make fonts[/].")
    for note in report.notes:
        console.print(f"  [dim]{note}[/]")

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim", width=22)
    table.add_column()
    mode = "measured (real font metrics)" if report.measured else "character wrap (fallback)"
    table.add_row("mode", mode)
    table.add_row("experience", f"{report.experience_lines} lines")
    table.add_row("worst project trio", f"{report.worst_trio} lines")
    if report.band_widths:
        table.add_row(
            "column holds over",
            f"{report.band_widths[0]:.0f}–{report.band_widths[1]:.0f}pt "
            f"(live {Frame.load().bullet_width:.1f}pt)",
        )
    if report.slack_narrow is not None:
        colour = "green" if report.slack_narrow >= 6 else "yellow"
        table.add_row(
            "margin for error",
            f"[{colour}]{report.slack_narrow:.1f}pt narrower[/] before a bullet gains a line "
            f"(the direction that breaks the page)",
        )
    if report.slack_wide is not None:
        table.add_row(
            "",
            f"[dim]{report.slack_wide:.1f}pt wider before one loses a line — cosmetic[/]",
        )
    console.print(table)

    if verbose:
        costs = Table("project", "lines", box=None, pad_edge=False)
        for pid, cost in sorted(report.project_costs.items(), key=lambda kv: (-kv[1], kv[0])):
            costs.add_row(pid, f"{cost}" if cost == 3 else f"[yellow]{cost}[/]")
        console.print(costs)

    for problem in report.over_budget:
        console.print(f"  [red]OVER BUDGET[/]  {problem}")
    for drift in report.drifts:
        console.print(f"  [red]DRIFT[/]  {drift}")

    if report.ok:
        console.print(
            f"\n  [green]no drift[/] — {len(report.project_costs)} projects and every "
            f"experience bullet measure exactly as declared"
        )
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def preview(
    family: str = typer.Argument(
        "data_science", help="role family: quantitative, machine_learning, data_engineering, …"
    ),
    out: str = typer.Option("output/preview", "--out", "-o", help="where to write"),
    docx: bool = typer.Option(True, "--docx/--no-docx", help="also write the ATS variant"),
    variants: bool = typer.Option(
        False, "--variants", help="swap in each bullet's first pre-approved variant"
    ),
) -> None:
    """Render a tailored CV for a role family, with no job and no model.

    This is the deterministic path end to end: pick three projects, pack the
    skill rows, draw the frozen frame. Useful for seeing what the selector does,
    and for checking the design against the reference before anything else runs.
    """
    import pathlib

    from .families import FAMILIES, signal_for
    from .layout import Frame
    from .loader import load_profile
    from .render_docx import assert_same_content, render_docx
    from .render_pdf import render_cv
    from .selector import build_config

    if family not in FAMILIES:
        console.print(f"[red]unknown family[/] {family!r} — one of: {', '.join(sorted(FAMILIES))}")
        raise typer.Exit(2)

    profile, frame = load_profile(), Frame.load()
    signal = signal_for(family)
    chosen_variants = (
        {b.id: (b.variants[0].id if b.variants else None) for b in profile.experience_bullets}
        if variants else None
    )
    config = build_config(profile, frame, signal, variants=chosen_variants)

    directory = pathlib.Path(out)
    pdf = render_cv(profile, frame, config, directory / f"Simon_Sazonov_CV_{family}.pdf")
    console.print(f"\n[bold]{family}[/] — {pdf.page_count} page, {pdf.body_lines} body lines, "
                  f"{frame.page_height - pdf.bottom_y:.0f}pt to spare")

    table = Table("score", "project", "why", box=None, pad_edge=False)
    for slot in config.projects:
        table.add_row(f"{slot.score:.1f}", slot.id, config.rationale.get(slot.id, ""))
    console.print(table)
    for row in frame.skill_rows:
        items = config.skills.get(row["id"]) or []
        joined = ", ".join(items)
        console.print(
            f"  [dim]{row['id']:<18}[/] {len(joined):>3}/{row['max_chars']} chars, "
            f"{frame.line_cost(joined)}/{row['lines']} lines — {joined[:70]}…"
        )

    if docx:
        result = render_docx(profile, config, directory / f"Simon_Sazonov_CV_{family}.docx")
        divergences = assert_same_content(pdf.printed, result.bullets)
        if divergences:
            console.print(f"  [red]PDF and DOCX disagree[/] on {len(divergences)} bullet(s) — "
                          f"one was built from something other than the profile ids")
            for text in divergences[:3]:
                console.print(f"     {text[:90]}")
            raise typer.Exit(1)
        console.print(f"  [green]✓[/] DOCX matches the PDF on all {len(result.bullets)} bullets")

    for note in pdf.notes:
        console.print(f"  [dim]note  {note}[/]")
    for warning in pdf.warnings:
        console.print(f"  [yellow]warn  {warning}[/]")
    console.print(f"\n  wrote [bold]{directory}[/]")


def _router(run_dir=None):
    """A task router with the run's cache and trace attached."""
    import pathlib

    from ..config import get_settings
    from .llm import TaskRouter

    settings = get_settings()
    cache = pathlib.Path(settings.cache_dir) / "apply"
    return TaskRouter(
        settings=settings,
        cache_dir=cache,
        trace_path=(pathlib.Path(run_dir) / "trace.jsonl") if run_dir else None,
    )


@app.command()
def research(
    company: list[str] = typer.Argument(None, help="employer names, as the posting spells them"),
    job: list[int] = typer.Option([], "--job", "-j", help="repeatable: take the employer off these postings"),
    top: int = typer.Option(0, "--top", help="take the employers off the top N shortlisted postings"),
    force: bool = typer.Option(False, "--force", help="ignore the TTL and research again"),
    show: bool = typer.Option(False, "--show", help="print what is cached and research nothing"),
    dry_run: bool = typer.Option(False, "--dry-run", help="search and filter, print the sources, call no model"),
) -> None:
    """Research what an employer actually builds, from their own pages.

    Fills the COMPANY NOTES slot the planner reads when `profile/companies.yaml`
    has no entry for this employer. A hand-written entry always wins, and nothing
    here ever writes to `profile/`.

        pitwall apply research Anthropic Helsing
        pitwall apply research Meetdavis --dry-run     # what the model would see
        pitwall apply research --show                  # what is already cached
    """
    from .research import dry_run_report, research_companies

    names = _research_targets(list(company or []), list(job or []), top)
    if show:
        _show_research()
        return
    if not names:
        console.print("[red]no employer named[/] — pass a name, --job, --top or --show")
        raise typer.Exit(2)

    if dry_run:
        failures = 0
        for name in names:
            failures += 0 if dry_run_report(name, console=console) else 1
        raise typer.Exit(1 if failures else 0)

    router = _router(run_dir="run/research")
    reports = research_companies(names, router=router, force=force,
                                 on_company=lambda n, r: _print_research(n, r))
    prompt, completion = router.tokens
    console.print(f"\n  [dim]{prompt + completion} tokens[/]")
    raise typer.Exit(0 if all(r.usable or r.outcome == "cached" for r in reports) else 1)


def _research_targets(names: list[str], job_ids: list[int], top: int) -> list[str]:
    """Employer names from any of the three ways of naming them, deduped."""
    from .jobs import companies_for, shortlist

    out = list(names)
    if job_ids:
        out += [c for c in companies_for(job_ids).values() if c]
    if top:
        out += [str(r.get("company_name") or "") for r in shortlist(limit=top)]
    seen, unique = set(), []
    for name in out:
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(name.strip())
    return unique


def _show_research() -> None:
    from .research_cache import ResearchCache

    records = ResearchCache().all()
    if not records:
        console.print("  [dim]nothing researched yet[/]")
        return
    for r in records:
        state = ("[green]verified[/]" if r.verified and r.independent
                 else "[yellow]self-verified[/]" if r.verified
                 else "[red]not verified[/]")
        console.print(f"\n[bold]{r.company}[/]  {state}  [dim]{r.fetched_at} · {r.outcome}[/]")
        console.print(f"  {r.notes}" if r.notes else "  [dim](nothing citable was found)[/]")
        for url in r.sources[:6]:
            console.print(f"    [dim]{url}[/]")
        for why in r.rejected:
            console.print(f"    [red]dropped[/] {why}")


def _print_research(name: str, report) -> None:
    state = ("[green]verified[/]" if report.record.verified and report.record.independent
             else "[yellow]self-verified[/]" if report.record.verified
             else "[red]not verified[/]")
    console.print(f"\n[bold]{name}[/]  {state}  [dim]{report.outcome}[/]")
    if report.record.notes:
        console.print(f"  {report.record.notes}")
    else:
        console.print("  [dim](nothing citable — the letter will say nothing about them)[/]")
    for sentence in report.record.sentences:
        console.print(f"    [dim]{sentence.get('source', '')}[/]")
    for why in report.record.rejected:
        console.print(f"    [red]dropped[/] {why}")
    for note in report.notes:
        console.print(f"    [dim]{note}[/]")
    console.print(
        f"  [dim]{report.queries_run} queries, {report.hits_seen} hits, "
        f"{report.kept} kept, {report.dropped} dropped, {report.pages_fetched} pages read[/]"
    )


@app.command()
def jobs(
    limit: int = typer.Option(15, "--limit", "-n"),
    min_score: float = typer.Option(60.0, "--min-score"),
) -> None:
    """The best-scoring open postings, so you have a job id to work with."""
    from .jobs import shortlist

    rows = shortlist(limit=limit, min_score=min_score)
    if not rows:
        console.print(f"[yellow]nothing scoring at or above {min_score}[/] — try --min-score lower")
        raise typer.Exit(0)
    table = Table("id", "score", "", "company", "title", box=None, pad_edge=False)
    for row in rows:
        table.add_row(
            str(row["id"]), f"{row['score']:.0f}", row["label"] or "",
            (row["company_name"] or "—")[:24], (row["title"] or "")[:52],
        )
    console.print(table)


@app.command()
def analyze(
    job: int = typer.Option(..., "--job", "-j", help="job id, from `pitwall apply jobs`"),
) -> None:
    """What this posting asks for, what you can prove, and what you cannot.

    One model call, no generation. The gap report is worth having on its own:
    knowing precisely which requirements are matched, partial and missing is
    useful before a single document exists.
    """
    from .analyzer import analyze_job
    from .jobs import existing_application, load_job
    from .loader import load_profile
    from .matcher import match
    from .truth import build_truth

    row = load_job(job)
    if row is None:
        console.print(f"[red]no job {job}[/] — try `pitwall apply jobs`")
        raise typer.Exit(1)

    profile = load_profile()
    console.print(f"\n[bold]{row['title']}[/]")
    company = row.get("posting_company") or row.get("company_name") or "—"
    console.print(f"[dim]{company} · {row.get('location') or '—'} · "
                  f"{len(row.get('description') or '')} chars of posting[/]\n")

    if prior := existing_application(str(company)):
        console.print(f"  [yellow]you have already applied to {company}[/] "
                      f"({len(prior)} application(s)) — make this a deliberate choice\n")

    router = _router()
    analysis = analyze_job(row, router)
    result = match(analysis, profile)

    console.print(f"  role family [bold]{analysis.role_family}[/] · seniority "
                  f"[bold]{analysis.seniority}[/] · language {analysis.language}")
    if analysis.truncated:
        console.print("  [dim]the posting was truncated for analysis — head plus requirements[/]")
    if analysis.rejected:
        console.print(f"  [yellow]{len(analysis.rejected)} invented requirement(s) dropped[/]: "
                      f"{', '.join(t for t, _ in analysis.rejected[:5])} "
                      f"[dim](quote not found in the posting)[/]")

    table = Table("", "requirement", "evidence", box=None, pad_edge=False)
    for hit in result.matched:
        table.add_row("[green]+[/]", hit.requirement,
                      f"{hit.skill_id} · {hit.confidence} · {len(hit.evidence_ids)} bullet(s)")
    for near in result.partial:
        table.add_row("[yellow]~[/]", near.requirement, f'only as: "{near.approved_phrasing}"')
    for gap in result.missing:
        note = "declared gap" if gap.declared_gap else "no evidence"
        table.add_row("[red]-[/]", gap.requirement, f"{note} ({gap.criticality})")
    console.print(table)

    if result.tag_only:
        console.print(f"\n  [dim]matched on bullet tags with no skills.yaml record: "
                      f"{', '.join(result.tag_only)}[/]")
        console.print("  [dim]real evidence, but it cannot print in a skills row — "
                      "worth adding records for[/]")
    if result.unmatched_terms:
        console.print(f"  [dim]not in the taxonomy: {', '.join(result.unmatched_terms[:8])}[/]")
    for gate in analysis.hard_gates:
        console.print(f"  [yellow]gate[/]  {gate}")

    truth = build_truth(profile, missing_requirements=result.missing_terms, job=row)
    if truth.contradicted_gaps:
        console.print("\n  [dim]proposed as gaps, but the profile proves them — NOT forbidden:[/]")
        for term, why in truth.contradicted_gaps:
            console.print(f"     [dim]{term} — {why}[/]")
    console.print(f"\n  forbidden for this run: {len(truth.forbidden_claims)} claim(s), "
                  f"{len(truth.forbidden_topics)} topic(s)")

    verdict = (
        "[red]a stretch[/] — the honest read is what is missing, not an application"
        if result.is_stretch else "[green]worth applying for[/]"
    )
    console.print(f"  coverage: required [bold]{result.coverage_required:.0%}[/], "
                  f"preferred {result.coverage_preferred:.0%}, "
                  f"overall [bold]{result.overall:.0%}[/] — {verdict}")
    prompt, completion = router.tokens
    cached = sum(1 for c in router.calls if c.cached)
    cost = (f"{cached} cached call(s), no tokens spent" if cached and not (prompt + completion)
            else f"{prompt + completion} tokens")
    console.print(f"  [dim]{cost}[/]\n")


@app.command()
def batch(
    job: list[int] = typer.Option([], "--job", "-j", help="repeatable; or use --from-file"),
    from_file: str = typer.Option("", "--from-file", help="a file of job ids, one per line"),
    name: str = typer.Option("", "--name", help="ledger name; defaults to today"),
    shards: int = typer.Option(1, "--shards", help="how many workers share this batch"),
    index: int = typer.Option(0, "--shard", help="which worker this process is (0-based)"),
    out: str = typer.Option("output", "--out", "-o"),
    cv: bool = typer.Option(True, "--cv/--no-cv"),
    audit: bool = typer.Option(False, "--audit/--no-audit", help="off by default in a batch"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    report_only: bool = typer.Option(False, "--report", help="print the ledger, generate nothing"),
) -> None:
    """Generate many applications unattended, then say which need attention.

    Every job lands in one of two piles — `ready`, meaning every deterministic
    check passed, or a named reason. The model audit is off by default here: it
    takes about ten minutes a letter on this machine and it does not decide the
    pile.

    Three workers in parallel, each with its own key in its own environment:

        GROQ_API_KEY=$KEY_A pitwall apply batch --from-file jobs.txt --shards 3 --shard 0 &
        GROQ_API_KEY=$KEY_B pitwall apply batch --from-file jobs.txt --shards 3 --shard 1 &
        GROQ_API_KEY=$KEY_C pitwall apply batch --from-file jobs.txt --shards 3 --shard 2 &
        wait; pitwall apply batch --name <same-name> --report
    """
    import datetime as dt
    import pathlib

    from .batch import format_report, load_ledger, run_batch, split

    label = name or f"batch-{dt.date.today().isoformat()}"
    ids = list(job)
    if from_file:
        text = pathlib.Path(from_file).read_text(encoding="utf-8")
        ids += [int(line) for line in text.split() if line.strip().lstrip("-").isdigit()]

    if report_only:
        rows = load_ledger(label)
        letters = {}
        for row in rows:
            path = pathlib.Path(out) / row.directory / row.letter
            if row.directory and row.letter and path.exists():
                letters[row.directory] = path.read_text(encoding="utf-8")
        console.print(f"\n[bold]{label}[/]\n")
        console.print(format_report(rows, letters=letters))
        return

    if not ids:
        console.print("[yellow]no job ids — pass --job N (repeatable) or --from-file[/]")
        raise typer.Exit(1)

    mine = split(ids, shards, index)
    console.print(
        f"\n[bold]{label}[/] · shard {index + 1} of {shards} · "
        f"{len(mine)} of {len(set(ids))} job(s)\n"
    )

    def say(row) -> None:
        colour = {"ready": "green", "review": "yellow"}.get(row.status, "red")
        console.print(
            f"  [{colour}]{row.status:<8}[/] {row.job_id:>6}  {row.company[:30]:<32}"
            f"{row.seconds:>6.0f}s  {'; '.join(row.reasons)[:60]}"
        )

    rows = run_batch(
        ids, name=label, shards=shards, index=index, out=out, cv=cv,
        audit=audit, resume=resume, on_row=say,
    )
    console.print("\n" + format_report(rows))
    console.print(f"\n  ledger: run/batch/{label}/  ·  re-read it any time with --report --name {label}\n")


@app.command()
def generate(
    job: int = typer.Option(..., "--job", "-j", help="job id, from `pitwall apply jobs`"),
    cv: bool = typer.Option(True, "--cv/--no-cv"),
    letter: bool = typer.Option(True, "--letter/--no-letter"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the plan, write nothing"),
    out: str = typer.Option("output", "--out", "-o"),
    hint: str = typer.Option("", "--plan-hint", help='e.g. "lead with the thesis, not the finance work"'),
    no_llm: bool = typer.Option(False, "--no-llm", help="deterministic CV only, no model at all"),
    record: bool = typer.Option(False, "--record", help="add a SHORTLISTED row to the tracker"),
    audit: bool = typer.Option(True, "--audit/--no-audit", help="run the model fact check"),
) -> None:
    """Generate a tailored CV and cover letter for one posting.

    Nothing reaches `output/` unless the deterministic gates pass. A rejected
    draft goes to `run/` marked REJECTED and this exits non-zero — a human
    reading three highlighted spans is a good outcome; silently shipping a
    plausible lie is the only truly bad one.
    """
    from ..config import get_settings
    from ..db import get_conn
    from .packager import record_application
    from .pipeline import generate_application

    with console.status("working…") as status:
        result = generate_application(
            job, out=out, cv=cv, letter=letter, hint=hint, audit=audit,
            no_llm=no_llm, dry_run=dry_run,
            progress=lambda key, message, frac: status.update(
                f"[dim]{frac:.0%}[/] {message}"
            ),
        )

    if result.error:
        console.print(f"[red]{result.error}[/]")
        raise typer.Exit(1)

    console.print(f"\n[bold]{result.title}[/]\n[dim]{result.company} · "
                  f"{result.job.get('location') or '—'}[/]\n")
    if result.match is not None:
        console.print(f"  coverage [bold]{result.match.overall:.0%}[/] · "
                      f"{len(result.truth.forbidden_claims)} forbidden claim(s)"
                      + ("  [red]stretch[/]" if result.match.is_stretch else ""))
        if result.truth.contradicted_gaps:
            console.print(f"  [dim]{len(result.truth.contradicted_gaps)} proposed gap(s) the "
                          f"profile disproves — not forbidden[/]")

    table = Table("score", "project", "why", box=None, pad_edge=False)
    for slot in result.config.projects:
        table.add_row(f"{slot.score:.1f}", slot.id, result.config.rationale.get(slot.id, ""))
    console.print(table)
    if result.plan is not None:
        console.print(f"\n  theme [italic]{result.plan.main_theme}[/]")
        console.print(f"  evidence: {' → '.join(result.plan.evidence_order)}")

    if dry_run:
        console.print("\n  [dim]--dry-run: nothing written[/]\n")
        raise typer.Exit(0)

    # No longer an early exit. The letter IS written when a claim survives — under
    # `..._DO_NOT_SEND.txt`, with a banner — so the CV, the note and the folder are
    # all there to report. The exit code still says something went wrong.
    if result.blocked:
        console.print(
            "\n  [red]DO NOT SEND[/] — a claim in this letter is not supported by the "
            "profile and the repair did not fix it"
        )
        for violation in result.letter.blocking[:6]:
            console.print(f"     [red]{violation.code}[/] {violation.detail} — {violation.span!r}")

    if result.cv_pdf:
        console.print(f"  [green]✓[/] CV — {result.cv_pdf.name}")
    if result.letter_path:
        console.print(f"  [green]✓[/] letter — {result.letter.words} words, "
                      f"{result.letter.attempts} attempt(s)"
                      + ("" if result.fact is None else f", {result.fact.summary()}"))
    if result.needs_review and result.fact is not None:
        console.print(
            f"  [yellow]NEEDS REVIEW[/] — after {result.fact.rounds} rewrite round(s) the "
            f"auditor ({result.fact.auditor or 'n/a'}) still questions "
            f"{len(result.fact.failures)} sentence(s). It is high-recall and imprecise, so "
            f"read these and judge for yourself:"
        )
        for verdict in result.fact.failures[:5]:
            console.print(f"     [yellow]{verdict.verdict}[/] "
                          f"{verdict.span or verdict.sentence[:78]!r}")
    if result.fact is not None and not result.fact.independent:
        console.print("  [yellow]the fact check was not independent[/] — "
                      "the writer graded itself, so read the letter yourself")
    for note in result.notes:
        console.print(f"  [dim]note  {note}[/]")

    if record and result.directory is not None:
        application_id = record_application(
            get_conn(get_settings()), job=result.job, company=result.company,
            cv_version=result.cv_pdf.name if result.cv_pdf else "",
            letter_version=result.letter_path.name if result.letter_path else "",
            directory=result.directory,
        )
        console.print(f"  [green]✓[/] tracked as application {application_id} (SHORTLISTED)")

    console.print(f"\n  wrote [bold]{result.directory}[/]  [dim]({result.tokens} tokens)[/]")
    if result.directory is not None:
        console.print(
            f"  [dim]what to check, and what was verified: "
            f"{result.directory / 'REVIEW.md'}[/]\n"
        )
    if result.blocked:
        raise typer.Exit(1)


@app.command("voice-eval")
def voice_eval(
    job: list[int] = typer.Option([], "--job", "-j", help="repeatable; jobs you know well"),
    show: bool = typer.Option(False, "--show", help="print the trend and stop"),
    out: str = typer.Option("run/voice", "--out", "-o"),
) -> None:
    """Generate letters, read them, score them — and keep the score.

    Two numbers per letter. NATURALNESS (1-5) asks whether a person would have
    written it; a letter can be perfectly true and still read like a machine.
    EDIT BURDEN (%) asks how much you would change before sending, and that is
    the number that decides whether this is worth using. The bar is 30% or below.

    The scores are yours. Nothing here grades itself.
    """
    import pathlib

    from .analyzer import analyze_job
    from .jobs import load_job
    from .letter import PROMPT_VERSION, draft_and_check
    from .loader import load_profile
    from .matcher import match
    from .planner import plan_letter
    from .truth import build_truth
    from .voice_eval import Rating, append, format_trend, load

    if show or not job:
        console.print(f"\n  prompt version in use: [bold]{PROMPT_VERSION}[/]\n")
        console.print(format_trend(load(), how="run this with --job N on a few jobs you know well"))
        console.print()
        if not job:
            console.print("  [dim]pass --job N (repeatable) to generate and rate a round[/]\n")
        raise typer.Exit(0)

    profile = load_profile()
    directory = pathlib.Path(out) / PROMPT_VERSION
    directory.mkdir(parents=True, exist_ok=True)
    console.print(f"\n  prompt version [bold]{PROMPT_VERSION}[/] · {len(job)} letter(s)\n")

    drafted = []
    for job_id in job:
        row = load_job(job_id)
        if row is None:
            console.print(f"  [red]no job {job_id}[/], skipping")
            continue
        company = str(row.get("posting_company") or row.get("company_name") or "Unknown")
        router = _router()
        analysis = analyze_job(row, router)
        result = match(analysis, profile)
        truth = build_truth(
            profile,
            missing_requirements=[
                *result.missing_terms, *(row.get("match", {}).get("missing_skills") or [])
            ],
            job=row,
        )
        plan, blocks = plan_letter(profile, analysis, result, truth, router, row)
        letter, fact = draft_and_check(profile, plan, blocks, truth, router, row)
        path = directory / f"{job_id}_{company.replace(' ', '_')}.txt"
        path.write_text(letter.text, encoding="utf-8")
        drafted.append((row, company, letter, fact, path))
        flag = " [yellow]NEEDS REVIEW[/]" if fact and fact.needs_review else ""
        console.print(f"  [green]✓[/] {job_id} {company} — {letter.words} words → {path}{flag}")

    if not drafted:
        raise typer.Exit(1)

    console.print("\n  Read each letter, then score it. [dim]Ctrl-C to stop and rate later.[/]\n")
    for row, company, letter, fact, path in drafted:
        console.print(f"[bold]{company}[/] — {row.get('title') or ''}")
        console.print(f"[dim]{path}[/]")
        natural = typer.prompt("  naturalness, 1-5 (would a person have written this?)", type=int)
        burden = typer.prompt("  edit burden %, 0-100 (how much would you change?)", type=int)
        note = typer.prompt("  one line on what is off (optional)", default="", show_default=False)
        append(Rating(
            prompt_version=PROMPT_VERSION,
            job_id=int(row["id"]),
            company=company,
            role=str(row.get("title") or ""),
            naturalness=max(1, min(natural, 5)),
            edit_burden_pct=max(0, min(burden, 100)),
            words=letter.words,
            rounds=(fact.rounds if fact else 1),
            needs_review=bool(fact and fact.needs_review),
            note=note,
        ))
        console.print()

    console.print(format_trend(load()))
    console.print()


@app.command()
def tidy(
    out: str = typer.Option("output", "--out", "-o"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="show what would move, move nothing"
    ),
) -> None:
    """One folder per job in `output/`, plus an index of what is sendable.

    Superseded runs move to `output/_archive/` — nothing is deleted, because
    the older letters are the record of how the prompt got better and their
    grades in `run/voice_eval.csv` still name them.
    """
    import pathlib

    from .tidy import (
        ARCHIVE_DIR,
        CHANGES_DIR,
        READY_DIR,
        apply_plan,
        build_plan,
        index_markdown,
        sendable,
    )

    root = pathlib.Path(out)
    plan = build_plan(root)
    if not plan.keep and not plan.supersede:
        console.print(f"[dim]nothing in {root}/ yet[/]")
        raise typer.Exit(0)

    table = Table(
        "goes to", "job", "company", "role", "v",
        box=None, pad_edge=False, show_lines=False,
    )
    for generation in sorted(plan.keep, key=lambda g: (not sendable(g), g.company.lower())):
        ready = sendable(generation)
        table.add_row(
            f"[green]{READY_DIR}[/]" if ready else f"[yellow]{CHANGES_DIR}[/]",
            str(generation.job_id or "—"), generation.company[:20],
            generation.title[:26], generation.prompt_version,
        )
    console.print(f"\n[bold]{plan.jobs} job(s) in {root}/[/]")
    console.print(table)

    if plan.supersede:
        console.print(
            f"\n  {len(plan.supersede)} older run(s) "
            f"{'would move' if dry_run else 'moved'} to [bold]{root}/{ARCHIVE_DIR}/[/]"
        )
    if dry_run:
        console.print("\n  [dim]--dry-run: nothing moved, no index written[/]\n")
        raise typer.Exit(0)

    apply_plan(plan, root)
    # Rebuilt from disk AFTER the move, so the index describes what is there
    # rather than what was there.
    (root / "README.md").write_text(index_markdown(build_plan(root)), encoding="utf-8")
    console.print(f"  index: [bold]{root}/README.md[/]\n")
