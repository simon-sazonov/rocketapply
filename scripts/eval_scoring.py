#!/usr/bin/env python
"""Scoring evaluation — §12.2 of PITWALL_PLAN.md.

*The most important 90 minutes of this project.* Hand-label 60 candidates from
a real run with `relevant: yes/no` ("would you actually message this person?"),
save them to `tests/fixtures/eval_set.csv`, and this prints::

                          precision  recall   F1    LLM calls   cost
    heuristics only          0.61     0.93   0.74        0     $0.0000
    LLM only                 0.88     0.85   0.86       60     $0.0031
    heuristics -> LLM        0.91     0.84   0.87       21     $0.0011

Watch recall in the first row: if the heuristic threshold is throwing away real
people, keep it at or above 0.90. Re-run this every time you change the prompt,
the rubric or a threshold. **Tuning without this is guessing.**

    python scripts/eval_scoring.py                     # score the saved set
    python scripts/eval_scoring.py bootstrap --n 60    # draft a set from the DB
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.db import open_repos  # noqa: E402
from pitwall.models import Company, Role  # noqa: E402
from pitwall.scoring.heuristics import passes, score_candidate  # noqa: E402
from pitwall.scoring.llm import LLMScorer, batched  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=False)
console = Console()

#: The tracked fixture carries synthetic identities (§3.4) so the repository can
#: be shared. The owner's real labelled set lives in `data/`, which is
#: git-ignored, and is preferred when it is there — a threshold tuned against
#: pseudonyms would still be tuned against the right *evidence*, but there is no
#: reason to throw away the real thing on the machine that has it.
PRIVATE_EVAL_CSV = ROOT / "data" / "eval_set_real.csv"
SHARED_EVAL_CSV = ROOT / "tests" / "fixtures" / "eval_set.csv"
EVAL_CSV = PRIVATE_EVAL_CSV if PRIVATE_EVAL_CSV.exists() else SHARED_EVAL_CSV
EVAL_COLUMNS = [
    "candidate_id", "full_name", "serp_title", "serp_snippet", "profile_url",
    "target_company", "company_aliases", "company_negatives",
    "target_role", "role_aliases", "role_family", "role_seniority", "role_keywords",
    "location", "evidence_count", "template_count",
    # Provenance (§1.3). SearXNG returns a different snippet shape from `ddgs`,
    # so a label collected on one does not transfer to the other and a threshold
    # swept against the wrong provider is a number about a world that no longer
    # exists. Recorded per row so `sweep` can refuse to recommend from stale labels.
    "serp_provider",
    "relevant",
]

#: A labelled row with no `serp_provider` predates the column, so it is `ddgs`
#: by construction: that is the only provider v1 ever used.
LEGACY_PROVIDER = "ddgs"


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    llm_calls: int = 0
    cost: float = 0.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def add(self, predicted: bool, actual: bool) -> None:
        if predicted and actual:
            self.tp += 1
        elif predicted and not actual:
            self.fp += 1
        elif not predicted and actual:
            self.fn += 1
        else:
            self.tn += 1


def _load_eval(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        console.print(
            f"[red]{path} not found.[/] Run "
            "[bold]python scripts/eval_scoring.py bootstrap[/] to draft one from your database, "
            "then hand-label the `relevant` column with yes/no."
        )
        raise typer.Exit(1)
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    labelled = [r for r in rows if str(r.get("relevant", "")).strip().lower() in
                {"yes", "no", "y", "n", "1", "0", "true", "false"}]
    if not labelled:
        console.print(f"[red]{path} has no labelled rows[/] — fill in the `relevant` column.")
        raise typer.Exit(1)
    if len(labelled) < len(rows):
        console.print(f"[yellow]{len(rows) - len(labelled)} unlabelled row(s) skipped[/]")
    return labelled


def _provenance(rows: list[dict[str, str]]) -> Counter[str]:
    return Counter((r.get("serp_provider") or LEGACY_PROVIDER).strip() for r in rows)


def _active_provider(settings) -> str:
    """The provider the next real run would use — chain[0]."""
    from pitwall.search import build_chain

    try:
        return build_chain(settings)[0].name
    except Exception:  # noqa: BLE001 - a preview must not fail on a dead provider
        return settings.search_providers[0] if settings.search_providers else "?"


def _truth(row: dict[str, str]) -> bool:
    return str(row.get("relevant", "")).strip().lower() in {"yes", "y", "1", "true"}


def _company(row: dict[str, str]) -> Company:
    return Company(
        name=row.get("target_company") or "unknown",
        aliases=row.get("company_aliases", ""),
        negative_keywords=row.get("company_negatives", ""),
    )


def _role(row: dict[str, str]) -> Role:
    return Role(
        title=row.get("target_role") or "unknown",
        aliases=row.get("role_aliases", ""),
        family=row.get("role_family") or "data_science",
        seniority=row.get("role_seniority", ""),
        keywords=row.get("role_keywords", ""),
    )


def _candidate(row: dict[str, str]) -> dict[str, object]:
    return {
        "id": int(row.get("candidate_id") or 0),
        "profile_url": row.get("profile_url", ""),
        "full_name": row.get("full_name", ""),
        "headline": row.get("serp_snippet", ""),
        "location": row.get("location") or None,
        "evidence_count": int(row.get("evidence_count") or 0),
        "template_count": int(row.get("template_count") or 0),
        "current_company": "",
    }


@app.command()
def bootstrap(
    n: int = typer.Option(60, help="how many candidates to draft"),
    out: Path = typer.Option(EVAL_CSV, help="where to write the template"),
    provider: str | None = typer.Option(
        None,
        help="only candidates whose evidence came from this SERP provider "
        "(e.g. searxng) — see §1.3, labels do not transfer between providers",
    ),
) -> None:
    """Draft a labelling worksheet from the database, spanning the score range.

    Pass `--provider searxng` after a fresh sweep to build the re-labelling
    worksheet §1.3 requires. The existing 60 rows were labelled on `ddgs` snippet
    shapes and cannot be reused: SearXNG's `content` field is not DuckDuckGo's
    `body`, so the same person reads differently to the heuristic layer.
    """
    settings = get_settings()
    repos = open_repos(settings)
    where = "WHERE sr.provider = ?" if provider else ""
    args = (provider,) if provider else ()
    rows = [
        dict(r)
        for r in repos.conn.execute(
            f"""
            SELECT c.id AS candidate_id, c.full_name, c.profile_url, c.location,
                   c.evidence_count, c.template_count,
                   sr.title AS serp_title, sr.snippet AS serp_snippet,
                   sr.provider AS serp_provider,
                   co.name AS target_company, co.aliases AS company_aliases,
                   co.negative_keywords AS company_negatives,
                   ro.title AS target_role, ro.aliases AS role_aliases,
                   ro.family AS role_family, ro.seniority AS role_seniority,
                   ro.keywords AS role_keywords, s.heuristic_score
              FROM candidates c
              JOIN scores s ON s.candidate_id = c.id
              LEFT JOIN companies co ON co.id = s.company_id
              LEFT JOIN roles ro     ON ro.id = s.role_id
              LEFT JOIN candidate_sources cs ON cs.candidate_id = c.id
              LEFT JOIN search_results sr    ON sr.id = cs.search_result_id
             {where}
             GROUP BY c.id
             ORDER BY s.heuristic_score DESC
            """,
            args,
        )
    ]
    if not rows:
        hint = f" from provider {provider!r}" if provider else ""
        console.print(f"[red]no scored candidates yet{hint}[/] — run the pipeline first")
        raise typer.Exit(1)

    # spread the sample across the score range rather than taking the top N
    step = max(1, len(rows) // n)
    sample = rows[::step][:n]

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVAL_COLUMNS)
        writer.writeheader()
        for row in sample:
            payload = {k: (row.get(k) or "") for k in EVAL_COLUMNS}
            for json_col in ("company_aliases", "company_negatives", "role_aliases",
                             "role_seniority", "role_keywords"):
                from pitwall.db import loads

                payload[json_col] = "|".join(loads(row.get(json_col)))
            payload["relevant"] = ""
            writer.writerow(payload)
    provenance = Counter(r.get("serp_provider") or "?" for r in sample)
    console.print(
        f"[green]wrote {len(sample)} rows[/] to {out}\n"
        f"provenance: {', '.join(f'{k}={v}' for k, v in provenance.most_common())}\n"
        "Now fill in the [bold]relevant[/] column with yes/no — "
        "'would I actually message this person?' — and re-run this script.\n"
        "[dim]Label from the snippet as shown, not from the profile: the heuristic "
        "layer only ever sees what is in these two columns.[/]"
    )


@app.command()
def evaluate(
    path: Path = typer.Option(EVAL_CSV, help="the labelled eval set"),
    threshold: float | None = typer.Option(None, help="heuristic threshold to test"),
    strong: float | None = typer.Option(None, help="final-score cut-off counted as a match"),
    no_llm: bool = typer.Option(False, "--no-llm", help="heuristics rows only"),
) -> None:
    """Print precision / recall / F1 for each scoring strategy."""
    settings = get_settings()
    rows = _load_eval(path)
    thr = threshold if threshold is not None else settings.heuristic_threshold
    cut = strong if strong is not None else settings.band_possible

    heur_only = Metrics()
    llm_only = Metrics()
    combined = Metrics()

    heuristic_scores: dict[int, float] = {}
    for row in rows:
        result = score_candidate(
            _candidate(row), _company(row), _role(row),
            serp_title=row.get("serp_title", ""), serp_snippet=row.get("serp_snippet", ""),
        )
        heuristic_scores[int(row.get("candidate_id") or 0)] = result.score
        heur_only.add(passes(result, thr), _truth(row))

    scorer = None
    if not no_llm:
        try:
            scorer = LLMScorer(settings)
            _ = scorer.client
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]LLM unavailable ({exc}); heuristics only[/]")
            scorer = None

    llm_scores: dict[int, float] = {}
    if scorer is not None:
        from pitwall.pipeline import Pipeline

        pipe = Pipeline(settings)
        for batch in batched(rows, settings.llm_batch_size):
            payload = [
                {
                    "id": int(r.get("candidate_id") or 0),
                    "raw_title": r.get("serp_title", ""),
                    "raw_snippet": r.get("serp_snippet", ""),
                    "profile_url": r.get("profile_url", ""),
                }
                for r in batch
            ]
            outcome = scorer.score_batch(payload, _company(batch[0]), _role(batch[0]))
            llm_only.llm_calls += 1
            for verdict in outcome.verdicts:
                llm_scores[verdict.candidate_id] = pipe.final_score(
                    heuristic_scores.get(verdict.candidate_id, 0.0), verdict
                )
        llm_only.cost = scorer.estimated_cost()

        for row in rows:
            cid = int(row.get("candidate_id") or 0)
            llm_only.add(llm_scores.get(cid, 0.0) >= cut, _truth(row))

        # combined: heuristics gate first, LLM only on the survivors
        survivors = [r for r in rows if heuristic_scores[int(r["candidate_id"])] >= thr]
        combined.llm_calls = max(0, -(-len(survivors) // settings.llm_batch_size))
        combined.cost = llm_only.cost * (combined.llm_calls / max(1, llm_only.llm_calls))
        for row in rows:
            cid = int(row["candidate_id"])
            predicted = heuristic_scores[cid] >= thr and llm_scores.get(cid, 0.0) >= cut
            combined.add(predicted, _truth(row))
    else:
        llm_only = combined = Metrics()

    table = Table(title=f"eval on {len(rows)} labelled candidates (threshold={thr:g}, cut={cut:g})")
    table.add_column("strategy")
    table.add_column("precision", justify="right")
    table.add_column("recall", justify="right")
    table.add_column("F1", justify="right")
    table.add_column("LLM calls", justify="right")
    table.add_column("cost", justify="right")
    for label, m in (
        ("heuristics only", heur_only),
        ("LLM only", llm_only),
        ("heuristics → LLM", combined),
    ):
        if label != "heuristics only" and scorer is None:
            continue
        recall_style = "[red]" if label == "heuristics only" and m.recall < 0.90 else ""
        table.add_row(
            label,
            f"{m.precision:.2f}",
            f"{recall_style}{m.recall:.2f}",
            f"{m.f1:.2f}",
            str(m.llm_calls),
            f"${m.cost:.4f}",
        )
    console.print(table)
    if heur_only.recall < 0.90:
        console.print(
            f"[red]Heuristic recall is {heur_only.recall:.2f} — below the 0.90 floor. "
            f"The threshold ({thr:g}) is throwing away real people; lower it.[/]"
        )
    else:
        console.print(f"[green]Heuristic recall {heur_only.recall:.2f} ≥ 0.90 — the gate is safe.[/]")


def _warn_if_stale(rows: list[dict[str, str]], settings) -> tuple[str, str] | None:
    """§1.3's guard rail: labels from one provider do not transfer to another.

    Returns `(labelled_on, active_now)` when they disagree, else None. `sweep`
    uses it to refuse to recommend a threshold rather than to refuse to compute
    one — seeing the numbers is useful, adopting them silently is the mistake.
    """
    counts = _provenance(rows)
    if not counts:
        return None
    labelled_on = counts.most_common(1)[0][0]
    active = _active_provider(settings)
    if len(counts) > 1:
        console.print(
            f"[yellow]mixed provenance in the eval set: "
            f"{', '.join(f'{k}={v}' for k, v in counts.most_common())}[/]"
        )
    if labelled_on != active:
        console.print(
            f"[yellow]These labels were collected on `{labelled_on}`; the active "
            f"provider is `{active}`.[/]"
        )
        return labelled_on, active
    return None


@app.command()
def relabel(
    companies: str = typer.Option(
        None, help="comma-separated; default = the 8 highest-tier active companies"
    ),
    max_queries: int = typer.Option(60, help="query budget for the sweep"),
    n: int = typer.Option(60, help="rows to draft for labelling"),
    out: Path = typer.Option(None, help="worksheet path; default is per-provider"),
    skip_sweep: bool = typer.Option(False, "--skip-sweep", help="worksheet only"),
) -> None:
    """The §1.3 sequence, as one command: sweep, then draft the worksheet.

    `tests/fixtures/eval_set.csv` was hand-labelled on `ddgs` snippet shapes.
    SearXNG returns a different `content` field, so those labels do not transfer
    and any threshold swept against them is a number about the old world. This
    runs a small sweep on the *current* provider and drafts a worksheet from the
    rows it produced — step 3, the hand labelling, is yours.

    It makes real search requests and adds real rows to the database.
    """
    settings = get_settings()
    provider = _active_provider(settings)
    out = out or (ROOT / "tests" / "fixtures" / f"eval_set_{provider}.csv")

    if not skip_sweep:
        from pitwall.pipeline import Pipeline, RunParams, RunStats

        pipe = Pipeline(settings)
        picked = [c.strip() for c in companies.split(",")] if companies else None
        if picked is None:
            picked = [c["name"] for c in pipe.repos.companies.all()][:8]
        console.print(
            f"sweeping [cyan]{len(picked)}[/] companies on [cyan]{provider}[/] "
            f"with a {max_queries}-query budget:\n  " + ", ".join(picked)
        )
        params = RunParams(
            companies=picked, max_queries=max_queries, use_llm=False,
        )
        stats = RunStats()
        with pipe.tracked(params, stats, "relabel"):
            pipe.build_queries(params)
            pipe.search(params, stats)
            pipe.extract(params, stats)
            pipe.score_heuristics(params, stats)
        console.print(
            f"[green]{stats.queries_executed}[/] queries -> "
            f"[green]{stats.results_fetched}[/] SERP rows -> "
            f"[green]{stats.candidates_new}[/] new people "
            f"({stats.heuristic_scored} scored)"
        )
        for note in stats.notes:
            console.print(f"[yellow]note:[/] {note}")

    bootstrap(n=n, out=out, provider=provider)
    console.print(
        f"\n[bold]Next:[/] label the `relevant` column in {out}, then\n"
        f"  python scripts/eval_scoring.py sweep --path {out}\n"
        "and record the before/after precision and recall next to the value you adopt."
    )


@app.command()
def provenance(
    path: Path = typer.Option(EVAL_CSV, help="the labelled eval set"),
) -> None:
    """Which provider's SERP shapes was this eval set labelled against? (§1.3)"""
    settings = get_settings()
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    counts = _provenance(rows)
    labelled = sum(
        1 for r in rows
        if str(r.get("relevant", "")).strip().lower() in {"yes", "no", "y", "n", "1", "0"}
    )
    table = Table(title=f"{path.name}: {len(rows)} rows, {labelled} labelled")
    table.add_column("SERP provider")
    table.add_column("rows", justify="right")
    for name, n in counts.most_common():
        table.add_row(name, str(n))
    console.print(table)
    active = _active_provider(settings)
    console.print(f"active provider for the next run: [cyan]{active}[/]")
    if counts and counts.most_common(1)[0][0] != active:
        console.print(
            "[yellow]These do not match.[/] SearXNG's `content` field is not "
            "DuckDuckGo's `body`: the same person reads differently to the heuristic "
            "layer, so the labels do not transfer and the threshold has to be re-swept."
        )


@app.command()
def sweep(
    path: Path = typer.Option(EVAL_CSV, help="the labelled eval set"),
    thresholds: str = typer.Option("0,5,10,12,15,20,25,30,40", help="comma-separated"),
) -> None:
    """Precision/recall/F1 for the heuristic gate across several thresholds.

    Pick the highest threshold that still clears the 0.90 recall floor: that is
    the cheapest gate that is not throwing away real people.
    """
    settings = get_settings()
    rows = _load_eval(path)
    stale = _warn_if_stale(rows, settings)
    scored = {
        int(r.get("candidate_id") or 0): score_candidate(
            _candidate(r), _company(r), _role(r),
            serp_title=r.get("serp_title", ""), serp_snippet=r.get("serp_snippet", ""),
        )
        for r in rows
    }

    table = Table(title=f"heuristic threshold sweep on {len(rows)} labelled candidates")
    for column in ("threshold", "precision", "recall", "F1", "kept", "LLM calls saved"):
        table.add_column(column, justify="right" if column != "threshold" else "left")

    safe_rows: list[tuple[float, float]] = []   # (threshold, F1) clearing the floor
    for raw in thresholds.split(","):
        thr = float(raw)
        m = Metrics()
        kept = 0
        for row in rows:
            result = scored[int(row.get("candidate_id") or 0)]
            predicted = passes(result, thr)
            kept += int(predicted)
            m.add(predicted, _truth(row))
        saved = max(0, -(-len(rows) // settings.llm_batch_size) - -(-kept // settings.llm_batch_size))
        safe = m.recall >= 0.90
        style = "[green]" if safe else "[red]"
        table.add_row(
            f"{thr:g}", f"{m.precision:.2f}", f"{style}{m.recall:.2f}", f"{m.f1:.2f}",
            str(kept), str(saved),
        )
        if safe:
            safe_rows.append((thr, m.f1))
    console.print(table)
    if not safe_rows:
        console.print("[red]no threshold in this range clears the 0.90 recall floor[/]")
        return
    # The cheapest safe gate: the HIGHEST threshold that still clears the recall
    # floor without giving up meaningful F1. Equal F1 at a higher threshold means
    # the same quality for fewer LLM calls.
    best_f1 = max(f1 for _, f1 in safe_rows)
    best = max(thr for thr, f1 in safe_rows if f1 >= best_f1 - 0.01)
    if stale:
        console.print(
            f"[red]Not recommending a threshold.[/] These labels were collected on "
            f"`{stale[0]}` but the active provider is `{stale[1]}`, and the two return "
            "different snippet shapes — the numbers above describe a world that is no "
            "longer the one you search in.\n"
            "  1. sweep 5-10 companies on the new provider\n"
            "  2. [bold]python scripts/eval_scoring.py bootstrap "
            f"--provider {stale[1]} --out tests/fixtures/eval_set_{stale[1]}.csv[/]\n"
            "  3. hand-label it, then re-run this against that file\n"
            f"[dim](for reference only, it would have said {best:g}.)[/]"
        )
        return
    console.print(
        f"[green]Recommended: heuristic_threshold = {best:g}[/] "
        f"(F1 {dict(safe_rows)[best]:.2f}, recall ≥ 0.90, and the highest safe cut-off "
        f"so the fewest candidates reach the LLM). "
        f"Currently configured: {settings.heuristic_threshold:g}.\n"
        f"[dim]Record the before/after precision and recall in config/settings.yaml "
        f"alongside the value — a threshold with no numbers next to it is a guess.[/]"
    )


@app.command()
def corroboration(
    path: Path = typer.Option(EVAL_CSV, help="the labelled eval set"),
    per_query: str = typer.Option("0,1,2,3,4", help="comma-separated bonuses per extra query"),
    per_template: str = typer.Option("0,2,4,6", help="comma-separated bonuses per extra template"),
    threshold: float | None = typer.Option(None, help="heuristic threshold to hold fixed"),
) -> None:
    """Measure the §1.4 corroboration bonus instead of guessing it.

    285 of 384 people in the real database were surfaced by exactly one query,
    so agreement across queries — and especially across *templates*, which look
    at different corners of the web — is a strong unused signal. What it is
    worth in points is an empirical question, and this is how it gets answered.

    Read the table the same way as `sweep`: the recall floor is 0.90, and among
    the settings that clear it, more precision for the same recall is the win.
    """
    settings = get_settings()
    rows = _load_eval(path)
    stale = _warn_if_stale(rows, settings)
    thr = threshold if threshold is not None else settings.heuristic_threshold

    corroborated = sum(
        1 for r in rows
        if int(r.get("evidence_count") or 0) > 1 or int(r.get("template_count") or 0) > 1
    )
    if not corroborated:
        console.print(
            "[red]No row in this eval set has more than one source.[/] The bonus "
            "cannot be measured against it — draft a worksheet from a sweep wide "
            "enough that the same person is found twice."
        )
        raise typer.Exit(1)
    console.print(
        f"{corroborated} of {len(rows)} labelled rows have more than one source "
        f"(threshold held at {thr:g})"
    )

    table = Table(title="corroboration bonus sweep")
    for column in ("per query", "per template", "precision", "recall", "F1", "kept"):
        table.add_column(column, justify="right")

    best: tuple[float, float, float] | None = None   # (f1, precision, ...)
    rowset = []
    for q_raw in per_query.split(","):
        for t_raw in per_template.split(","):
            wq, wt = float(q_raw), float(t_raw)
            m = Metrics()
            kept = 0
            for row in rows:
                result = score_candidate(
                    _candidate(row), _company(row), _role(row),
                    serp_title=row.get("serp_title", ""),
                    serp_snippet=row.get("serp_snippet", ""),
                    weights={"query": wq, "template": wt,
                             "max": settings.corroboration_max},
                )
                predicted = passes(result, thr)
                kept += int(predicted)
                m.add(predicted, _truth(row))
            safe = m.recall >= 0.90
            style = "[green]" if safe else "[red]"
            table.add_row(
                f"{wq:g}", f"{wt:g}", f"{m.precision:.2f}",
                f"{style}{m.recall:.2f}", f"{m.f1:.2f}", str(kept),
            )
            rowset.append((wq, wt, m.precision, m.recall, m.f1))
    console.print(table)

    safe_rows = [r for r in rowset if r[3] >= 0.90]
    if not safe_rows:
        console.print("[red]nothing in this grid clears the 0.90 recall floor[/]")
        return
    baseline = next((r for r in rowset if r[0] == 0 and r[1] == 0), None)
    best = max(safe_rows, key=lambda r: (r[4], r[2]))
    if baseline and best[4] <= baseline[4] + 0.005:
        console.print(
            "[yellow]No corroboration weight beats the zero baseline on this set.[/] "
            "Leave scoring.corroboration at 0 — that is a result, not a failure."
        )
        return
    if stale:
        console.print(
            f"[red]Not recommending a weight.[/] These labels were collected on "
            f"`{stale[0]}` and the active provider is `{stale[1]}` — see §1.3."
        )
        return
    console.print(
        f"[green]Best: per_query = {best[0]:g}, per_template = {best[1]:g}[/] "
        f"(precision {best[2]:.2f}, recall {best[3]:.2f}, F1 {best[4]:.2f}"
        + (f" vs F1 {baseline[4]:.2f} at zero)" if baseline else ")")
        + "\nSet it in config/settings.yaml under scoring.corroboration and record "
        "these numbers next to it."
    )


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        evaluate()


if __name__ == "__main__":
    app()
