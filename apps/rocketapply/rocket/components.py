"""Pieces every screen shares: the navigation titles, headers, cards, the stage track."""

from __future__ import annotations

import html
import pathlib
from typing import Any

import streamlit as st

from .format import tokens


def nav_titles(counts: dict[str, int], generating: tuple[int, int] | None = None
               ) -> dict[str, str]:
    """Each workflow page's name in the navigation, with how many jobs wait there.

    The navigation is the process: a job's status anywhere in the app (Selected,
    In review, In process, Applied) is the name of the page it sits on, so there
    is one vocabulary, not a second row of stages repeating the first."""
    def titled(name: str, n: int) -> str:
        return f"{name} · {n:,}" if n else name

    return {
        "search": titled("Search", counts.get("found", 0)),
        "selected": titled("Selected", counts.get("selected", 0)),
        "generate": (f"Generate · {generating[0]}/{generating[1]}" if generating
                     else "Generate"),
        "review": titled("Review", counts.get("docs_ready", 0)),
        "applications": titled("Applications", counts.get("in_process", 0)
                               + counts.get("applied", 0)),
    }


def header(title: str, subtitle: str = "") -> None:
    st.markdown(f"## {title}")
    if subtitle:
        st.caption(subtitle)


def card(kicker: str, big: str, note: str = "") -> None:
    st.html(
        f'<div class="ra-card"><div class="ra-kicker">{html.escape(kicker)}</div>'
        f'<div class="ra-big">{html.escape(big)}</div>'
        + (f'<div class="ra-dim" style="font-size:0.88rem">{html.escape(note)}</div>' if note else "")
        + "</div>"
    )


def chips(items: list[str], kind: str = "") -> str:
    return "".join(f'<span class="ra-chip {kind}">{html.escape(str(i))}</span>' for i in items)


def empty(message: str, hint: str = "") -> None:
    st.html(
        f'<div class="ra-card" style="text-align:center;padding:2.2rem 1rem">'
        f'<div style="font-size:1.05rem;font-weight:600">{html.escape(message)}</div>'
        + (f'<div class="ra-dim" style="margin-top:0.3rem">{html.escape(hint)}</div>' if hint else "")
        + "</div>"
    )


def score_color(score: float | None) -> str:
    s = float(score or 0)
    return "ok" if s >= 75 else "accent" if s >= 60 else "warn" if s >= 45 else ""


def stage_track(stages: dict[str, str], keys: list[str]) -> str:
    dots = []
    for key in keys:
        state = stages.get(key, "")
        cls = {"done": "done", "start": "run", "skip": "skip", "error": "error"}.get(state, "")
        dots.append(f'<span class="ra-dot {cls}" title="{key}: {state or "waiting"}"></span>')
    return '<span class="ra-track">' + "".join(dots) + "</span>"


def budget_line(b: Any) -> str:
    return (f"{tokens(b.room)} of {tokens(b.daily_limit)} tokens left today · "
            f"{tokens(b.per_job)} per job ({b.basis})")


@st.cache_data(show_spinner=False, max_entries=64)
def pdf_page_png(path: str, mtime: float, scale: float = 3.0) -> bytes | None:
    """First page of a PDF as a PNG, cached by path and modification time.

    The PDF itself is vector text; only this preview has a resolution. At 3x
    (216 DPI) the page is ~1800 px wide, enough to stay sharp when a Retina
    screen stretches it across the column — 1.3x (94 DPI) read as blurry.
    """
    try:
        import io

        import pypdfium2 as pdfium

        page = pdfium.PdfDocument(path)[0]
        buf = io.BytesIO()
        page.render(scale=scale).to_pil().save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a preview is a nicety
        return None


def pdf_preview(path: str | pathlib.Path | None, *, caption: str = "",
                width: int | str = "stretch") -> None:
    if not path or not pathlib.Path(path).is_file():
        st.caption("No PDF yet.")
        return
    p = pathlib.Path(path)
    png = pdf_page_png(str(p), p.stat().st_mtime)
    if png:
        st.image(png, caption=caption or p.name, width=width)
    else:
        st.caption(f"Preview unavailable — {p.name}")
