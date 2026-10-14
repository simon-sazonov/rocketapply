"""The light theme, finished off.

Streamlit's own widgets take the palette from `.streamlit/config.toml`; this
adds what config cannot reach — stage dots, cards, the
width of the page — and nothing more. Minimal on purpose: one accent, soft
borders, generous space.
"""

from __future__ import annotations

import streamlit as st

BG = "#FAFAF9"
SURFACE = "#FFFFFF"
MUTED_BG = "#F2F2F0"
LINE = "#E7E5E4"
FG = "#1C1917"
FG_DIM = "#57534E"
FG_FAINT = "#A8A29E"
ACCENT = "#2563EB"
ACCENT_SOFT = "#DBEAFE"
OK = "#15803D"
WARN = "#B45309"
FAIL = "#B91C1C"

_CSS = f"""
<style>
:root {{
  --ra-bg: {BG}; --ra-surface: {SURFACE}; --ra-muted: {MUTED_BG}; --ra-line: {LINE};
  --ra-fg: {FG}; --ra-dim: {FG_DIM}; --ra-faint: {FG_FAINT};
  --ra-accent: {ACCENT}; --ra-accent-soft: {ACCENT_SOFT};
  --ra-ok: {OK}; --ra-warn: {WARN}; --ra-fail: {FAIL};
}}
/* Wide, but not edge to edge on a big monitor. */
.block-container {{ max-width: 1480px; padding-top: 4.4rem; padding-bottom: 4rem; }}
h1 {{ letter-spacing: -0.02em; }}
[data-testid="stCaptionContainer"] {{ color: var(--ra-dim); }}

/* ── cards and small text ── */
.ra-card {{ border: 1px solid var(--ra-line); border-radius: 0.8rem; padding: 1rem 1.1rem;
  background: var(--ra-surface); }}
.ra-kicker {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--ra-faint); margin-bottom: 0.2rem; }}
.ra-big {{ font-size: 1.6rem; font-weight: 650; font-variant-numeric: tabular-nums; }}
.ra-dim {{ color: var(--ra-dim); }}
.ra-faint {{ color: var(--ra-faint); }}
.ra-chip {{ display: inline-block; padding: 0.1rem 0.55rem; margin: 0 0.3rem 0.3rem 0;
  border-radius: 999px; background: var(--ra-muted); border: 1px solid var(--ra-line);
  font-size: 0.82rem; color: var(--ra-fg); white-space: nowrap; }}
.ra-chip.ok {{ background: #F0FDF4; border-color: #BBF7D0; color: var(--ra-ok); }}
.ra-chip.warn {{ background: #FFFBEB; border-color: #FDE68A; color: var(--ra-warn); }}
.ra-chip.fail {{ background: #FEF2F2; border-color: #FECACA; color: var(--ra-fail); }}
.ra-chip.accent {{ background: var(--ra-accent-soft); border-color: #BFDBFE; color: var(--ra-accent); }}

/* ── the stage track: eight dots, one per stage ── */
.ra-track {{ display: flex; gap: 0.35rem; align-items: center; }}
.ra-dot {{ width: 0.62rem; height: 0.62rem; border-radius: 999px; background: var(--ra-line); }}
.ra-dot.done {{ background: var(--ra-ok); }}
.ra-dot.run {{ background: var(--ra-accent); box-shadow: 0 0 0 3px var(--ra-accent-soft); }}
.ra-dot.skip {{ background: transparent; border: 1.5px dashed var(--ra-faint); }}
.ra-dot.error {{ background: var(--ra-fail); }}
.ra-stage {{ display: grid; grid-template-columns: 1.4rem 11rem 1fr; gap: 0.4rem;
  padding: 0.35rem 0; border-bottom: 1px dashed var(--ra-line); align-items: baseline; }}
.ra-stage:last-child {{ border-bottom: none; }}
.ra-stage .what {{ color: var(--ra-fg); font-weight: 550; }}
.ra-stage .out {{ color: var(--ra-dim); font-size: 0.9rem; min-width: 0; }}
.ra-stage .ra-chip {{ white-space: normal; }}
.ra-letter {{ white-space: pre-wrap; font-size: 0.92rem; line-height: 1.55;
  background: var(--ra-surface); border: 1px solid var(--ra-line); border-radius: 0.6rem;
  padding: 0.9rem 1rem; }}
.ra-quote {{ color: var(--ra-faint); font-size: 0.82rem; font-style: italic; }}
</style>
"""


def inject() -> None:
    st.html(_CSS)
