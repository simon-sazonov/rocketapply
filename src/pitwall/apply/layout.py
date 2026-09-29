"""The frozen CV frame, and the line-cost model that makes fitting a guarantee.

Everything that decides how much room a piece of text takes lives here, and both
the cost model and the renderer read it — so the arithmetic that proves the page
fits cannot drift from the code that draws it.

TWO MEASUREMENT MODES, ONE API. `line_cost` measures with real font metrics
(`pdfmetrics.stringWidth`) when `assets/fonts/` is present, which is a genuine
measurement rather than the spec's character-count estimate. Where the fonts are
absent — CI, a fresh clone — it falls back to the greedy character wrap in
`validate.wrap_lines`. `Frame.measured` says which mode is live, and
`pitwall apply calibrate` reports where the two disagree.

GEOMETRY. Solved against the reference PDF rather than guessed: the search for a
(font size, column width) pair reproducing all seven experience bullets' observed
line counts *and* keeping every project bullet on one line has a wide solution
band, and the values below sit in its interior — 13pt of slack to the nearest
edge. That the band is wide is the real finding: the frame is robust, not fitted.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from xml.sax import saxutils

from . import PROFILE_DIR, project_root
from .validate import ROW_LIMITS, load_yaml, wrap_lines

log = logging.getLogger(__name__)

__all__ = [
    "Style", "Frame", "FONT_DIR", "register_fonts", "fonts_available",
    "escape_markup", "paragraph_style",
]

FONT_DIR = project_root() / "assets" / "fonts"

#: Registered ReportLab name -> file. Machina Ultrabold is commercial and not
#: licensed for this use, so `layout.yaml` names Archivo Black as the substitute,
#: chosen once at the template level and never per application.
FONT_FILES: dict[str, str] = {
    "DMSans": "DMSans-400.ttf",
    "DMSans-Medium": "DMSans-500.ttf",
    "DMSans-Bold": "DMSans-700.ttf",
    "DMSans-Italic": "DMSans-400i.ttf",
    "DMSans-BoldItalic": "DMSans-700i.ttf",
    "Arimo": "Arimo-400.ttf",
    "Arimo-Bold": "Arimo-700.ttf",
    "ArchivoBlack": "ArchivoBlack-400.ttf",
}

#: What to draw with when the vendored fonts are missing. The page still renders
#: and the suite still runs; it just will not look like the reference.
FALLBACK = {
    "DMSans": "Helvetica",
    "DMSans-Medium": "Helvetica",
    "DMSans-Bold": "Helvetica-Bold",
    "DMSans-Italic": "Helvetica-Oblique",
    "DMSans-BoldItalic": "Helvetica-BoldOblique",
    "Arimo": "Helvetica",
    "Arimo-Bold": "Helvetica-Bold",
    "ArchivoBlack": "Helvetica-Bold",
}


@lru_cache(maxsize=1)
def register_fonts(font_dir: str | None = None) -> bool:
    """Register the vendored faces with ReportLab. True if all of them loaded.

    Cached: registration is global to the ReportLab process and repeating it is
    wasted work, not an error.
    """
    directory = pathlib.Path(font_dir) if font_dir else FONT_DIR
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:  # pragma: no cover — reportlab is a hard dependency
        log.warning("reportlab is not installed; line costs fall back to character wrap")
        return False

    missing = []
    for name, filename in FONT_FILES.items():
        path = directory / filename
        if not path.exists():
            missing.append(filename)
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(path)))
        except Exception as exc:  # noqa: BLE001 — a bad file must not abort the run
            log.warning("font %s did not load (%s)", filename, exc)
            missing.append(filename)

    if missing:
        log.warning(
            "%d of %d fonts missing from %s (%s) — rendering falls back to Helvetica "
            "and line costs fall back to character wrap. Run `make fonts`.",
            len(missing), len(FONT_FILES), directory, ", ".join(sorted(missing)[:3]),
        )
        return False
    return True


def fonts_available() -> bool:
    return register_fonts()


@dataclass(frozen=True)
class Style:
    """A font at a size. The unit the cost model and the renderer share."""

    font: str
    size: float
    leading: float = 0.0

    def resolved(self) -> str:
        """The name to hand ReportLab, after the fallback substitution."""
        return self.font if fonts_available() else FALLBACK.get(self.font, "Helvetica")

    def line_height(self) -> float:
        return self.leading or self.size * 1.32


def escape_markup(text: str) -> str:
    """Make `text` safe for ReportLab's paragraph parser.

    Not optional: six strings in the profile contain an ampersand — "F1 Data
    Warehouse & ML Platform", "Data Pipelines & ETL" — and Paragraph treats its
    input as markup, so an unescaped one raises mid-render. Escape at the
    boundary; never store escaped text.
    """
    return saxutils.escape(text)


@lru_cache(maxsize=64)
def paragraph_style(font: str, size: float, leading: float, align: int = 0):
    """A cached ReportLab style. Cached because building one per line-cost call
    over 22 projects and 1,140 combinations is measurable."""
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle(
        f"{font}-{size}-{leading}-{align}",
        fontName=font, fontSize=size, leading=leading or size * 1.27, alignment=align,
    )


def text_width(text: str, style: Style) -> float:
    """Width of `text` in points. Raises if ReportLab is unavailable."""
    from reportlab.pdfbase import pdfmetrics

    return pdfmetrics.stringWidth(text, style.resolved(), style.size)


def wrap(text: str, style: Style, width: float) -> list[str]:
    """Greedy word wrap by measured width — what a renderer actually does.

    A single word wider than the column gets its own line rather than an infinite
    loop; the renderer will overrun slightly and `calibrate` will say so.
    """
    lines: list[str] = []
    cur = ""
    for word in text.split():
        cand = word if not cur else cur + " " + word
        if text_width(cand, style) <= width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


@dataclass
class Frame:
    """The frozen frame as data. Loaded from `profile/layout.yaml`.

    Nothing here is decided at generation time. The model fills three slots
    inside this and nothing else.
    """

    raw: dict[str, Any]
    measured: bool = False

    # ── geometry, in points ──
    page_width: float = 595.28          # A4
    page_height: float = 841.89
    margin_left: float = 40.0
    margin_right: float = 23.28
    margin_top: float = 36.0
    margin_bottom: float = 28.0
    gutter_width: float = 75.0          # the narrow left column holding section labels
    gutter_gap: float = 12.0
    bullet_indent: float = 20.0         # bullet glyph to text

    # ── type ──
    body: Style = field(default_factory=lambda: Style("DMSans", 9.5))
    body_bold: Style = field(default_factory=lambda: Style("DMSans-Bold", 9.5))
    body_italic: Style = field(default_factory=lambda: Style("DMSans-Italic", 9.5))
    body_bold_italic: Style = field(default_factory=lambda: Style("DMSans-BoldItalic", 9.5))
    heading: Style = field(default_factory=lambda: Style("DMSans-Bold", 10.0))
    gutter: Style = field(default_factory=lambda: Style("Arimo-Bold", 8.5))
    name: Style = field(default_factory=lambda: Style("ArchivoBlack", 22.0))

    # ── character-wrap fallback widths, from layout.yaml ──
    wrap_width_chars: int = 99
    wrap_width_safe: int = 95

    @property
    def content_x(self) -> float:
        return self.margin_left + self.gutter_width + self.gutter_gap

    @property
    def content_width(self) -> float:
        return self.page_width - self.margin_right - self.content_x

    @property
    def bullet_width(self) -> float:
        """The column a bullet's text wraps inside — the number that decides
        every line count on the page."""
        return self.content_width - self.bullet_indent

    # ── construction ──

    @classmethod
    def load(cls, directory: pathlib.Path | str | None = None) -> Frame:
        raw = load_yaml(pathlib.Path(directory) if directory else PROFILE_DIR, "layout.yaml") or {}
        page = raw.get("page") or {}
        geom = raw.get("geometry") or {}
        frame = cls(
            raw=raw,
            measured=fonts_available(),
            wrap_width_chars=int(page.get("wrap_width_chars", 99)),
            wrap_width_safe=int(page.get("wrap_width_safe", 95)),
        )
        for key in (
            "page_width", "page_height", "margin_left", "margin_right",
            "margin_top", "margin_bottom", "gutter_width", "gutter_gap", "bullet_indent",
        ):
            if key in geom:
                setattr(frame, key, float(geom[key]))
        for key, attr in (
            ("body", "body"), ("body_bold", "body_bold"), ("body_italic", "body_italic"),
            ("body_bold_italic", "body_bold_italic"), ("heading", "heading"),
            ("gutter", "gutter"), ("name", "name"),
        ):
            spec = (geom.get("type") or {}).get(key)
            if isinstance(spec, dict) and "size" in spec:
                current: Style = getattr(frame, attr)
                setattr(frame, attr, Style(
                    spec.get("font", current.font),
                    float(spec["size"]),
                    float(spec.get("leading", 0) or 0),
                ))
        return frame

    # ── the line-cost model ──

    def line_cost(self, text: str, style: Style | None = None, width: float | None = None) -> int:
        """How many rendered lines `text` occupies.

        Where the fonts are present this ASKS THE RENDERER rather than modelling
        it: a real `Paragraph` is wrapped at the real width with the real font,
        and its line list is counted. So the number cannot disagree with what is
        drawn — which a character-count estimate, or even a hand-rolled
        stringWidth loop, eventually would. ReportLab does not break at hyphens
        where the source design does, and counting this way absorbs that for free.

        This is the invariant the whole frame rests on: because every cost is
        known before rendering, overflow is impossible by construction and the
        page-count assertion is a backstop rather than the mechanism.
        """
        if not text.strip():
            return 0
        if not self.measured:
            return wrap_lines(text, self.wrap_width_safe)
        style = style or self.body
        from reportlab.platypus import Paragraph

        para = Paragraph(
            escape_markup(text),
            paragraph_style(style.resolved(), style.size, style.line_height()),
        )
        para.wrap(width or self.bullet_width, 10_000)
        return max(1, len(para.blPara.lines))

    def char_cost(self, text: str) -> int:
        """The character-wrap answer, whatever mode is live. `calibrate` compares
        this against `line_cost` so the two can never silently diverge."""
        return wrap_lines(text, self.wrap_width_safe)

    def project_cost(self, bullets: list[str]) -> int:
        """1 title line + one cost per bullet. Always 3 for a well-formed project."""
        return 1 + sum(self.line_cost(b) for b in bullets)

    # ── budgets, straight from layout.yaml ──

    def _section(self, sid: str) -> dict[str, Any]:
        for section in self.raw.get("sections") or []:
            if section.get("id") == sid:
                return section
        return {}

    @property
    def section_order(self) -> list[str]:
        return list(self.raw.get("section_order") or
                    ["contact", "skills", "experience", "projects", "education"])

    @property
    def project_count(self) -> int:
        return int(self._section("projects").get("count", 3))

    @property
    def bullets_per_project(self) -> int:
        return int(self._section("projects").get("bullets_per_project", 2))

    @property
    def project_budget(self) -> int:
        return int(self._section("projects").get("line_budget", 11))

    @property
    def experience_budget(self) -> int:
        return int(self._section("experience").get("line_budget", 17))

    @property
    def experience_entries(self) -> list[dict[str, Any]]:
        return list(self._section("experience").get("entries") or [])

    @property
    def skill_rows(self) -> list[dict[str, Any]]:
        rows = self._section("skills").get("rows") or []
        return [dict(r) for r in rows]

    def row_limit(self, row_id: str) -> int:
        for row in self.skill_rows:
            if row.get("id") == row_id:
                return int(row.get("max_chars", ROW_LIMITS.get(row_id, 195)))
        return ROW_LIMITS.get(row_id, 195)

    @property
    def project_bullet_style(self) -> Style:
        """`layout.yaml` settles the styling inconsistency in the source design:
        F1's project bullets are upright and the other two italic, and since
        projects are now selected dynamically, per-project styling could not have
        survived anyway."""
        styling = (self.raw.get("bullet_styling") or {}).get("projects", "upright")
        return self.body_italic if styling == "italic" else self.body
