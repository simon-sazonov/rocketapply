#!/usr/bin/env python
"""Verify the Groq path end to end — §0.3 of PITWALL_V2_IMPLEMENTATION.md.

    make groq-check
    .venv/bin/python scripts/setup_groq.py            # list, verify, score a real batch
    .venv/bin/python scripts/setup_groq.py --list     # just the catalogue
    .venv/bin/python scripts/setup_groq.py --no-verify

Groq deprecates model IDs on short notice, so **nothing in PitWall hardcodes
one**. This script calls `GET /openai/v1/models`, checks the configured model
against the live answer, and then proves it on the same three-candidate batch
`setup_ollama.py` uses — including the McLaren Health Care trap, which is the
one result that tells you the rubric survived the trip.

Get a key at <https://console.groq.com> — free, no credit card. Free-plan limits
as of 29 Aug 2026 are 30 requests/minute, 1,000 requests/day and 8,000
tokens/minute for the chat models; the token ceiling, not the request ceiling,
is what paces a real sweep.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.models import Company, Role  # noqa: E402
from pitwall.scoring.llm import (  # noqa: E402
    STRICT_JSON_MODELS,
    LLMScorer,
    LLMUnavailable,
    list_models,
)

console = Console()

#: Chat models on Groq's free plan, smallest first. Only used to make the
#: catalogue print-out readable — the live `GET /models` answer is the truth.
_NOISE = ("whisper", "orpheus", "prompt-guard", "tts", "guard")


def _batch() -> list[dict[str, str | int]]:
    return [
        {
            "id": 1,
            "raw_title": "Jane Doe - Senior Data Scientist - McLaren Racing | LinkedIn",
            "raw_snippet": "Data Scientist at McLaren Racing. Race strategy tooling. Woking, England.",
            "profile_url": "https://www.linkedin.com/in/jane-doe",
        },
        {
            "id": 2,
            "raw_title": "David Brooks - Data Analyst - McLaren Health Care | LinkedIn",
            "raw_snippet": "Data Analyst at McLaren Health Care, Macomb, Michigan, United States",
            "profile_url": "https://www.linkedin.com/in/david-brooks",
        },
        {
            "id": 3,
            "raw_title": "Luca Rossi - Strategy Consultant | LinkedIn",
            "raw_snippet": "Ex-McLaren Racing race strategist, now at Bain & Company",
            "profile_url": "https://www.linkedin.com/in/luca-rossi",
        },
    ]


def main(
    provider: str = typer.Option("groq", help="any OpenAI-compatible provider"),
    list_only: bool = typer.Option(False, "--list", help="print the catalogue and stop"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="score one real batch"),
    model: str = typer.Option(None, help="check this model id instead of the configured one"),
) -> None:
    settings = get_settings()
    if model:
        settings = settings.with_overrides(llm_model=model)
    configured = settings.llm_config(provider)["model"]

    # ── 1. the catalogue ──
    try:
        available = list_models(settings, provider)
    except LLMUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        console.print(
            "\nGet a free key at [bold]https://console.groq.com[/] (no card), then:\n"
            "  GROQ_API_KEY=gsk_…   in .env"
        )
        raise typer.Exit(1) from exc

    chat = [m for m in available if not any(n in m.lower() for n in _NOISE)]
    table = Table(title=f"{len(available)} models reachable with this key")
    table.add_column("model id")
    table.add_column("strict JSON", justify="center")
    table.add_column("", justify="left")
    for m in chat:
        table.add_row(
            m,
            "✓" if m in STRICT_JSON_MODELS else "",
            "[green]← configured[/]" if m == configured else "",
        )
    console.print(table)

    # ── 2. does the configured model exist? ──
    if configured not in available:
        console.print(
            f"\n[red]LLM_MODEL / GROQ_MODEL is {configured!r}, which this key cannot reach.[/]\n"
            "Groq retires model IDs on short notice. Pick one from the table above and set\n"
            "  LLM_MODEL=<id>   in .env\n"
            "Prefer the smallest one that supports strict JSON — this is a classification\n"
            "task with every piece of evidence supplied in the prompt, not a knowledge task."
        )
        raise typer.Exit(1)
    console.print(f"[green]✓[/] {configured} exists")

    scorer = LLMScorer(settings, provider=provider)
    if scorer.supports_strict_json():
        console.print(
            "[green]✓[/] strict structured output: the model is constrained to the schema "
            "while decoding, so `needs_review` should effectively disappear"
        )
    else:
        console.print(
            f"[yellow]![/] {configured} has no strict JSON mode — falling back to "
            "`json_object` and the repair ladder. Workable, but a strict model is better."
        )
    if scorer.is_reasoning_model():
        console.print(
            f"[dim]reasoning model: reasoning_effort={settings.llm_reasoning_effort}, "
            "trace hidden (it would otherwise be billed against the same token budget)[/]"
        )

    if list_only or not verify:
        _next_steps(provider, configured)
        return

    # ── 3. one real batch, including the wrong-org trap ──
    console.print("\n[bold]Verifying with a real batch…[/]")
    company = Company(
        name="McLaren Racing",
        aliases="McLaren F1",
        negative_keywords="McLaren Health Care|McLaren Automotive|hospital",
        series="F1",
        country="UK",
    )
    role = Role(
        title="Data Scientist",
        aliases="Data Science|ML Engineer",
        family="data_science",
        seniority="graduate|junior|mid",
        keywords="Monte Carlo",
    )

    started = time.time()
    try:
        outcome = scorer.score_batch(_batch(), company, role)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]the call failed:[/] {exc}")
        raise typer.Exit(1) from exc
    elapsed = time.time() - started

    if not outcome.verdicts:
        console.print(
            f"[red]no usable verdicts[/] ({outcome.error or 'no error reported'})\n"
            f"[dim]{outcome.raw[:400]}[/]"
        )
        raise typer.Exit(1)

    results = Table(title=f"{len(outcome.verdicts)} verdicts in {elapsed:.1f}s")
    for column in ("id", "score", "current", "company_match", "role_match", "reason"):
        results.add_column(column, justify="right" if column in ("id", "score") else "left")
    for v in outcome.verdicts:
        results.add_row(
            str(v.candidate_id), f"{v.score:.0f}", str(v.is_current_employee),
            v.company_match, v.role_match, (v.reasons[0] if v.reasons else "")[:44],
        )
    console.print(results)

    by_id = {v.candidate_id: v for v in outcome.verdicts}
    trap = by_id.get(2)
    if trap and trap.company_match == "same_name_different_org":
        console.print("[green]✓[/] caught the McLaren Health Care trap (same_name_different_org)")
    elif trap and trap.score < 40:
        console.print(f"[green]✓[/] scored the McLaren Health Care trap low ({trap.score:.0f})")
    else:
        console.print(
            "[yellow]![/] the wrong-org trap was not clearly rejected — the model works, "
            "but check a few verdicts by hand before trusting a full sweep."
        )

    per_batch = elapsed / max(1, len(_batch())) * settings.llm_batch_size
    console.print(
        f"\n[dim]~{per_batch:.1f}s per full batch of {settings.llm_batch_size}; "
        f"{scorer.usage.tokens_in:,} in / {scorer.usage.tokens_out:,} out tokens this call. "
        "Groq's free plan allows 8,000 tokens/minute, which is the real pace limit.[/]"
    )
    _next_steps(provider, configured)


def _next_steps(provider: str, model: str) -> None:
    console.print(
        "\n[bold]Next:[/] put it first in the chain and score the backlog\n"
        f"  LLM_PROVIDER_CHAIN={provider},zai,deepseek,ollama\n"
        f"  LLM_MODEL={model}\n\n"
        "  .venv/bin/python scripts/run_pipeline.py score --limit 64 -v   # a first tranche\n"
        "  .venv/bin/python scripts/run_pipeline.py score                 # the rest\n"
    )


if __name__ == "__main__":
    typer.run(main)
