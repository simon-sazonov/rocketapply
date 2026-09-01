#!/usr/bin/env python
"""Prove the local SearXNG actually works — the three checks from §0.1.

    make searxng-check
    .venv/bin/python scripts/check_searxng.py [--base-url http://localhost:8888]

1. `format=json` returns JSON, not 403 or an HTML page.
2. An operator query (`site:linkedin.com/in "race engineer" "PREMA"`) returns
   results — the specific capability `ddgs` lacks and the reason this container
   exists.
3. Nothing important is in `unresponsive_engines`, which is what a captcha or a
   blocked engine looks like from the outside.

Exits non-zero on the first failure, with the remedy in the message. This is the
one script in the repo that is *supposed* to hit the network.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402

console = Console()

OPERATOR_QUERY = 'site:linkedin.com/in "race engineer" "PREMA"'


def _get(base_url: str, params: dict[str, str], timeout: float) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}/search?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        console.print(f"[red]cannot reach {base_url}[/] — {exc}")
        console.print("  start it with [bold]make searxng[/], then retry.")
        raise typer.Exit(2) from exc


def main(
    base_url: str = typer.Option(None, help="defaults to SEARXNG_BASE_URL"),
    timeout: float = typer.Option(45.0, help="seconds per request"),
    engines: str = typer.Option(None, help="comma-separated engines to pin the check to"),
) -> None:
    settings = get_settings()
    base_url = base_url or settings.searxng_base_url
    console.print(f"checking [cyan]{base_url}[/]\n")

    # ── 1. JSON format ──
    status, body = _get(base_url, {"q": "test", "format": "json"}, timeout)
    if status == 403 or body.lstrip().startswith("<"):
        console.print(f"[red]1. JSON format is disabled[/] (HTTP {status})")
        console.print(
            "   Add `json` to search.formats in docker/searxng/settings.yml and\n"
            "   restart: [bold]make searxng-stop && make searxng[/]"
        )
        raise typer.Exit(1)
    if status >= 400:
        console.print(f"[red]1. HTTP {status}[/]: {body[:200]}")
        raise typer.Exit(1)
    try:
        json.loads(body)
    except json.JSONDecodeError:
        console.print(f"[red]1. body is not JSON[/]: {body[:200]}")
        raise typer.Exit(1) from None
    console.print("[green]1. JSON format enabled[/] — /search?format=json answers JSON")

    # ── 2. the operator query ──
    params = {
        "q": OPERATOR_QUERY,
        "format": "json",
        "language": "en-GB",
        "safesearch": "0",
        "categories": "general",
    }
    if engines:
        params["engines"] = engines
    status, body = _get(base_url, params, timeout)
    data = json.loads(body) if status < 400 else {}
    results = data.get("results") or []
    dead = data.get("unresponsive_engines") or []

    if results:
        console.print(f"[green]2. operator query returned {len(results)} results[/]")
        table = Table(show_header=True, header_style="dim")
        table.add_column("#", justify="right", style="dim")
        table.add_column("engine(s)")
        table.add_column("url")
        for i, row in enumerate(results[:5], start=1):
            table.add_row(str(i), ",".join(row.get("engines") or []), (row.get("url") or "")[:80])
        console.print(table)
        profiles = sum(1 for r in results if "linkedin.com/in/" in (r.get("url") or ""))
        console.print(f"   {profiles} of {len(results)} are linkedin.com/in profile URLs")
    else:
        console.print("[red]2. operator query returned nothing[/]")

    # ── 3. engine health ──
    if dead:
        console.print(f"[yellow]3. {len(dead)} unresponsive engine(s)[/] — captcha or block:")
        for row in dead:
            name, reason = (row + ["?"])[:2] if isinstance(row, list) else (row, "?")
            console.print(f"   · {name}: {reason}")
        console.print(
            "   The adapter treats this as a soft failure and backs off, so it will not\n"
            "   be recorded as 'zero results'. Slow the sweep down with\n"
            "   search.searxng_queries_per_second in config/settings.yaml if it persists."
        )
    else:
        console.print("[green]3. every engine responded[/]")

    if not results:
        console.print(
            "\n[red]The operator query found nothing.[/] Likely causes, in order:\n"
            "  · google/bing are disabled on this instance (docker/searxng/settings.yml\n"
            "    re-enables them; upstream ships both off)\n"
            "  · SEARXNG_ENGINES names an engine this instance does not have\n"
            "  · every upstream engine is currently blocked — try again in a few minutes"
        )
        raise typer.Exit(1)

    console.print("\n[bold green]SearXNG is ready.[/] Put it first in SEARCH_PROVIDER_CHAIN.")


if __name__ == "__main__":
    typer.run(main)
