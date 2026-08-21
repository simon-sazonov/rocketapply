#!/usr/bin/env python
"""Prepare the local Qwen model PitWall's LLM stage runs on.

Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`) **ignores** per-request
`options`, so `num_ctx` cannot be set from the client — it has to come from the model
itself. Left unset, Ollama uses a 4,096-token default while a batch of 8 needs roughly
3,300, and it truncates **from the front**: the system prompt, i.e. the whole scoring
rubric, disappears and the model scores confidently against nothing. There is no error.

This script closes that hole by deriving a model with `num_ctx` raised, then proving the
result end to end on a real batch rather than asserting it.

    python scripts/setup_ollama.py            # pull, derive, verify
    python scripts/setup_ollama.py --check    # verify only, change nothing

Qwen2.5 rather than Qwen3: Qwen3 is a hybrid reasoning model whose thinking mode is on by
default and cannot be disabled reliably through Ollama. For a temperature-0 JSON
classifier those tokens are pure cost, and on CPU they are minutes per batch.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.models import Company, Role  # noqa: E402
from pitwall.scoring.llm import (  # noqa: E402
    LLMScorer,
    LLMUnavailable,
    ollama_reachable,
    parse_num_ctx,
)

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()

BASE_MODEL = "qwen2.5:7b"
DERIVED_MODEL = "pitwall-qwen2.5"
NUM_CTX = 8192

MODELFILE = f"""FROM {BASE_MODEL}

# PitWall sends ~1,700-token batches and allows 1,500 tokens of output. Ollama's
# default 4,096-token window leaves almost no headroom and truncates from the
# front, which would silently remove the system prompt.
PARAMETER num_ctx {NUM_CTX}

# Deterministic classification, not chat.
PARAMETER temperature 0
PARAMETER top_p 1
"""


def _api_root(settings) -> str:
    return settings.ollama_base_url.rstrip("/").removesuffix("/v1")


def _installed(root: str) -> dict[str, dict]:
    try:
        resp = httpx.get(f"{root}/api/tags", timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise typer.Exit(1) from exc
    return {m["name"]: m for m in resp.json().get("models", [])}


def _show(root: str, model: str) -> dict | None:
    resp = httpx.post(f"{root}/api/show", json={"model": model}, timeout=15.0)
    return resp.json() if resp.status_code == 200 else None


def _run(cmd: list[str]) -> None:
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        console.print(f"[red]{proc.stderr.strip() or proc.stdout.strip()}[/]")
        raise typer.Exit(1)


@app.command()
def main(
    check: bool = typer.Option(False, "--check", help="verify only; make no changes"),
    base: str = typer.Option(BASE_MODEL, help="the model to derive from"),
    name: str = typer.Option(DERIVED_MODEL, help="name of the derived model"),
    num_ctx: int = typer.Option(NUM_CTX, help="context window for the derived model"),
    skip_verify: bool = typer.Option(False, "--skip-verify", help="do not run a live batch"),
) -> None:
    """Pull the base model, derive one with a usable context window, and prove it works."""
    settings = get_settings()
    root = _api_root(settings)

    if not ollama_reachable(settings.ollama_base_url):
        console.print(
            f"[red]Ollama is not reachable at {root}.[/]\n"
            "Start it (`ollama serve`, or launch the Ollama app) and re-run this script."
        )
        raise typer.Exit(1)
    console.print(f"[green]✓[/] Ollama is up at {root}")

    installed = _installed(root)

    # ── 1. the base model ──
    if base not in installed:
        if check:
            console.print(f"[red]✗[/] {base} is not installed — run this script without --check")
            raise typer.Exit(1)
        console.print(f"[yellow]pulling {base} (~4.7 GB, this takes a few minutes)…[/]")
        _run(["ollama", "pull", base])
        installed = _installed(root)
    console.print(f"[green]✓[/] base model {base} present")

    # ── 2. the derived model, with a context window big enough for a full batch ──
    detail = _show(root, name)
    current_ctx = parse_num_ctx((detail or {}).get("parameters", "")) if detail else None

    if current_ctx == num_ctx:
        console.print(f"[green]✓[/] {name} already has num_ctx={num_ctx}")
    elif check:
        console.print(
            f"[red]✗[/] {name} " + (f"has num_ctx={current_ctx}" if detail else "does not exist")
            + f" — expected {num_ctx}. Re-run without --check."
        )
        raise typer.Exit(1)
    else:
        console.print(f"[yellow]creating {name} from {base} with num_ctx={num_ctx}…[/]")
        modelfile = MODELFILE.replace(f"FROM {BASE_MODEL}", f"FROM {base}").replace(
            f"num_ctx {NUM_CTX}", f"num_ctx {num_ctx}"
        )
        # `ollama create -f -` does not read stdin, so the Modelfile needs a real path.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Modelfile"
            path.write_text(modelfile, encoding="utf-8")
            proc = subprocess.run(
                ["ollama", "create", name, "-f", str(path)], text=True, capture_output=True
            )
        if proc.returncode != 0:
            console.print(f"[red]{proc.stderr.strip() or proc.stdout.strip()}[/]")
            raise typer.Exit(1)
        console.print(f"[green]✓[/] created {name} (reuses {base}'s blobs — no extra disk)")

    # ── 3. what PitWall will actually do ──
    scorer = LLMScorer(settings, provider="ollama")
    scorer.model = name
    scorer.cfg = {**scorer.cfg, "model": name}
    try:
        scorer.preflight()
    except LLMUnavailable as exc:
        console.print(f"[red]preflight failed:[/] {exc}")
        raise typer.Exit(1) from exc

    table = Table(title="ready")
    table.add_column("setting")
    table.add_column("value", justify="right")
    table.add_row("model", name)
    table.add_row("context window", f"{num_ctx:,} tokens")
    table.add_row("batch prompt (est.)", f"{scorer.estimated_prompt_tokens():,} tokens")
    table.add_row("max output", f"{settings.llm_max_tokens:,} tokens")
    table.add_row("required context", f"{scorer.required_context_tokens():,} tokens")
    table.add_row("request timeout", f"{settings.llm_timeout_seconds:g}s")
    console.print(table)

    if skip_verify:
        _print_next_steps(settings, name)
        return

    # ── 4. one real batch, including the trap the whole design exists to catch ──
    console.print("\n[bold]Verifying with a real batch (a minute or two on CPU)…[/]")
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
    batch = [
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

    started = time.time()
    outcome = scorer.score_batch(batch, company, role)
    elapsed = time.time() - started

    if not outcome.verdicts:
        console.print(
            f"[red]the model returned no usable verdicts[/] "
            f"({outcome.error or 'no error reported'})"
        )
        raise typer.Exit(1)

    results = Table(title=f"{len(outcome.verdicts)} verdicts in {elapsed:.0f}s")
    for column in ("id", "score", "current", "company_match", "role_match", "reason"):
        results.add_column(column, justify="right" if column in ("id", "score") else "left")
    by_id = {v.candidate_id: v for v in outcome.verdicts}
    for v in outcome.verdicts:
        results.add_row(
            str(v.candidate_id), f"{v.score:.0f}", str(v.is_current_employee),
            v.company_match, v.role_match, (v.reasons[0] if v.reasons else "")[:44],
        )
    console.print(results)

    # the whole point of the design: the wrong-org trap must be caught
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

    per_batch = elapsed / max(1, len(batch)) * settings.llm_batch_size
    console.print(
        f"\n[dim]~{per_batch:.0f}s per full batch of {settings.llm_batch_size} at this rate.[/]"
    )
    _print_next_steps(settings, name)


def _print_next_steps(settings, name: str) -> None:
    console.print(
        "\n[bold]Next:[/] point .env at it and score the backlog\n"
        f"  LLM_PROVIDER=ollama\n"
        f"  OLLAMA_MODEL={name}\n\n"
        "  python scripts/run_pipeline.py score --limit 64 -v   # a first tranche\n"
        "  python scripts/run_pipeline.py score                 # the rest\n"
    )


if __name__ == "__main__":
    app()
