"""Pieces every screen shares: the process strip, headers, cards, the stage track."""

from __future__ import annotations

import html
import pathlib
from typing import Any

import streamlit as st

from . import data
from .format import tokens

#: (key in stage_counts, label, page, query) — the process, in order.
STRIP = (
    ("found", "Found", "views/search.py", None),
    ("selected", "Selected", "views/selected.py", None),
    ("docs_ready", "Documents ready", "views/review.py", None),
    ("in_process", "In process", "views/applications.py", {"tab": "in_process"}),
    ("applied", "Applied", "views/applications.py", {"tab": "applied"}),
)


def process_strip() -> None:
    """Found → Selected → Documents ready → In process → Applied, each a link.

    Page links rather than anchors: an anchor reloads the browser tab, which
    starts a new session and forgets the search filters."""
    try:
        counts = data.stage_counts()
    except Exception:  # noqa: BLE001 - a fresh install with no jobs yet
        counts = {}
    with st.container(key="ra_strip", horizontal=True, gap=None):
        for i, (key, label, page, query) in enumerate(STRIP):
            arrow = "" if i == len(STRIP) - 1 else ""
            st.page_link(page, label=f"**{counts.get(key, 0):,}** · {label}{arrow}",
                         query_params=query, width="stretch")


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
def pdf_page_png(path: str, mtime: float, scale: float = 1.3) -> bytes | None:
    """First page of a PDF as a PNG, cached by path and modification time."""
    try:
        import io

        import pypdfium2 as pdfium

        page = pdfium.PdfDocument(path)[0]
        buf = io.BytesIO()
        page.render(scale=scale).to_pil().save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a preview is a nicety
        return None


def pdf_preview(path: str | pathlib.Path | None, *, caption: str = "") -> None:
    if not path or not pathlib.Path(path).is_file():
        st.caption("No PDF yet.")
        return
    p = pathlib.Path(path)
    png = pdf_page_png(str(p), p.stat().st_mtime)
    if png:
        st.image(png, caption=caption or p.name, width="stretch")
    else:
        st.caption(f"Preview unavailable — {p.name}")
