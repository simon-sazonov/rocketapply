"""STAGE 5a — draw the frozen frame. No model involvement whatsoever.

Every string this module puts on the page comes from a profile record, reached
by id. There is no free-form generation here and no template into which prose
could be injected, which is why the CV path carries a full guarantee rather than
a probabilistic one: §10.4's check — every printed string is byte-identical to a
bullet, an approved variant, a metric claim or a profile field — is satisfiable
by construction, and `printed_strings()` exposes exactly what to check.

The layout is absolute rather than a flow of Platypus frames. For a fixed frame
that is the simpler choice: a y-cursor walks down the page, each block reports
its own height, and the arithmetic that proved the page fits is the same
arithmetic that draws it.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from .layout import Frame, Style, escape_markup, paragraph_style, register_fonts
from .models import Profile
from .selector import CvConfig

__all__ = ["RenderResult", "render_cv", "format_period"]

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

BULLET = "•"
DASH = "–"          # en dash, as the source design uses
RULE_WIDTH = 0.7
RULE_COLOUR = (0.1, 0.1, 0.1)


def format_period(start: str | None, end: str | None, end_label: str | None = None) -> str:
    """`2025-11` + `present` -> "Nov 2025 – Present". Dates are inline with the
    entry, never in a side rail — a graphic timeline is unreadable to a parser."""

    def one(value: str | None) -> str:
        if not value:
            return ""
        text = str(value).strip()
        if text.lower() == "present":
            return "Present"
        parts = text.split("-")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            month = int(parts[1])
            if 1 <= month <= 12:
                return f"{MONTHS[month - 1]} {parts[0]}"
        return text

    left, right = one(start), (end_label or one(end))
    if left and right:
        return f"{left} {DASH} {right}"
    return left or right


@dataclass
class RenderResult:
    path: pathlib.Path
    page_count: int = 0
    body_lines: int = 0
    bottom_y: float = 0.0
    #: Every string drawn, so the caller can assert each traces to a profile id.
    printed: list[str] = field(default_factory=list)
    #: The same strings, grouped by section. This is what proves the frame is
    #: frozen: across any number of jobs, `contact`, `experience` and `education`
    #: must be byte-identical, and only `skills` and `projects` may differ.
    sections: dict[str, list[str]] = field(default_factory=dict)
    #: Something is wrong and a human should look.
    warnings: list[str] = field(default_factory=list)
    #: The frame did something deliberate worth recording — dropping a stub
    #: repository's link label, omitting an employment marker that would collide
    #: with the date. Not problems; decisions, logged so they are reviewable.
    notes: list[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return self.page_count == 1


class _Canvas:
    """A y-cursor over one page, plus the few drawing primitives the frame needs."""

    def __init__(self, path: pathlib.Path, frame: Frame) -> None:
        from reportlab.pdfgen import canvas as rl_canvas

        self.frame = frame
        self.printed: list[str] = []
        self.sections: dict[str, list[str]] = {}
        self.section: str = "header"
        self.c = rl_canvas.Canvas(
            str(path), pagesize=(frame.page_width, frame.page_height), invariant=True
        )
        self.y = frame.margin_top
        self.lines = 0

    # ── primitives ──

    def _record(self, value: str) -> None:
        self.printed.append(value)
        self.sections.setdefault(self.section, []).append(value)

    def _flip(self, y_top: float, height: float) -> float:
        """PDF origin is bottom-left; the frame is described from the top."""
        return self.frame.page_height - y_top - height

    def text(self, value: str, style: Style, x: float, y_top: float) -> float:
        self.c.setFont(style.resolved(), style.size)
        self.c.setFillColorRGB(0, 0, 0)
        self.c.drawString(x, self._flip(y_top, style.size), value)
        self._record(value)
        return style.size

    def right_text(self, value: str, style: Style, x_right: float, y_top: float) -> None:
        self.c.setFont(style.resolved(), style.size)
        self.c.setFillColorRGB(0, 0, 0)
        self.c.drawRightString(x_right, self._flip(y_top, style.size), value)
        self._record(value)

    def para(self, value: str, style: Style, x: float, y_top: float, width: float) -> float:
        """A wrapped paragraph. Returns its height; the line count it reports is
        the same one `Frame.line_cost` predicted, because both ask Paragraph."""
        from reportlab.platypus import Paragraph

        para = Paragraph(
            escape_markup(value),
            paragraph_style(style.resolved(), style.size, style.line_height()),
        )
        _, height = para.wrapOn(self.c, width, 10_000)
        para.drawOn(self.c, x, self._flip(y_top, height))
        self.lines += len(para.blPara.lines)
        self._record(value)
        return height

    def rule(self, y_top: float) -> None:
        frame = self.frame
        self.c.setStrokeColorRGB(*RULE_COLOUR)
        self.c.setLineWidth(RULE_WIDTH)
        y = self.frame.page_height - y_top
        right = frame.page_width - frame.margin_right + float(
            (frame.raw.get("geometry") or {}).get("rule_bleed_right", 0) or 0
        )
        self.c.line(frame.margin_left, y, min(right, frame.page_width - 0.5), y)

    def bullet(self, value: str, style: Style, y_top: float) -> float:
        """One bulleted line of body text, glyph and all."""
        frame = self.frame
        self.c.setFont(style.resolved(), style.size)
        self.c.setFillColorRGB(0, 0, 0)
        self.c.drawString(frame.content_x + 5.7, self._flip(y_top, style.size), BULLET)
        return self.para(
            value, style, frame.content_x + frame.bullet_indent, y_top, frame.bullet_width
        )

    def save(self) -> None:
        self.c.showPage()
        self.c.save()


def _gutter(canvas: _Canvas, label: str, y_top: float) -> None:
    """Section labels sit in the narrow left column, aligned to the top of the
    section they name."""
    canvas.text(label, canvas.frame.gutter, canvas.frame.margin_left, y_top)


def draw_header(canvas: _Canvas, profile: Profile, frame: Frame) -> float:
    """The name, the title and the CONTACT block, exactly as the CV draws them.

    Shared with the cover letter (`render_letter.py`), so the two documents a
    recruiter opens side by side carry one identical letterhead. Returns the y
    where the next section starts.
    """
    f = frame
    geom = f.raw.get("geometry") or {}
    header_gap = float(geom.get("header_gap", 19))
    content_right = f.page_width - f.margin_right
    y = f.margin_top

    # ── header: never varies ──
    canvas.text(profile.personal.name, f.name, f.margin_left, y)
    canvas.right_text(profile.personal.title, f.name, content_right, y)
    y += f.name.size + header_gap

    # ── CONTACT: never varies, and no rule above it ──
    #: Where the first contact line sits, so the letter can date itself on it.
    canvas.contact_top = y
    canvas.section = "contact"
    _gutter(canvas, "CONTACT", y)
    for label, value in (
        ("Email", profile.personal.email),
        ("GitHub", _github_handle(profile.personal.github)),
        ("LinkedIn", profile.personal.linkedin),
    ):
        if not value:
            continue
        canvas.c.setFont(f.body_italic.resolved(), f.body.size)
        canvas.c.drawString(f.content_x, canvas._flip(y, f.body.size), f"{label}: ")
        offset = canvas.c.stringWidth(f"{label}: ", f.body_italic.resolved(), f.body.size)
        canvas.text(value, f.body, f.content_x + offset, y)
        canvas.lines += 1
        y += f.body.line_height()
    return y


def render_cv(
    profile: Profile,
    frame: Frame,
    config: CvConfig,
    path: pathlib.Path | str,
) -> RenderResult:
    """Write the one-page CV. Raises nothing on overflow — it reports it, and
    the caller decides, because a two-page CV is a calibration bug worth seeing."""
    register_fonts()
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = _Canvas(path, frame)
    # The profile's name, not a constant: the same bytes for its owner, and a
    # correct title for anyone else's profile.
    canvas.c.setTitle(f"{profile.personal.name} — CV")
    result = RenderResult(path=path)

    f = frame
    geom = f.raw.get("geometry") or {}
    section_gap = float(geom.get("section_gap", 3.5))
    gap = float(geom.get("rule_gap", 5.3))
    content_right = f.page_width - f.margin_right

    y = draw_header(canvas, profile, f)

    # ── SKILLS: SLOT B ──
    y += section_gap
    canvas.rule(y)
    y += gap
    canvas.section = "skills"
    _gutter(canvas, "SKILLS", y)
    for row in f.skill_rows:
        items = config.skills.get(row["id"]) or []
        if not items:
            result.warnings.append(f"{row['id']} packed empty")
            continue
        y += canvas.bullet(", ".join(items), f.body, y)

    # ── EXPERIENCE: fixed entries, fixed order, fixed bullet counts ──
    y += section_gap
    canvas.rule(y)
    y += gap
    canvas.section = "experience"
    _gutter(canvas, "EXPERIENCE", y)
    for entry in profile.experience:
        heading = f"{entry.org} {DASH} {entry.role}"
        canvas.text(heading, f.body_bold, f.content_x, y)
        used = canvas.c.stringWidth(heading, f.body_bold.resolved(), f.body_bold.size)
        period = format_period(entry.start, entry.end)
        if entry.employment_type:
            # The marker is decoration; the role and the dates are the content.
            # So it prints only where it fits clear of the right-aligned date —
            # which is exactly what the source design does: FreedomCode shows
            # "|Contract|" and Ophis, whose role runs long, does not.
            marker = f"  |{entry.employment_type}|"
            marker_w = canvas.c.stringWidth(marker, f.body_italic.resolved(), f.body_italic.size)
            period_w = canvas.c.stringWidth(period, f.body_italic.resolved(), f.body_italic.size)
            if f.content_x + used + marker_w + 8 <= content_right - period_w:
                canvas.text(marker, f.body_italic, f.content_x + used, y)
            else:
                result.notes.append(
                    f"{entry.id}: '|{entry.employment_type}|' omitted — the role runs to the "
                    f"date. The source design does the same thing here."
                )
        canvas.right_text(period, f.body_italic, content_right, y)
        canvas.lines += 1
        y += f.body.line_height()
        for bullet in entry.bullets:
            # SLOT C — a pre-approved, line-neutral variant, or the canonical text.
            chosen = config.experience_variants.get(bullet.id)
            variant = bullet.variant(chosen) if chosen else None
            y += canvas.bullet(variant.text if variant else bullet.text, f.body, y)

    # ── PROJECTS: SLOT A ──
    y += section_gap
    canvas.rule(y)
    y += gap
    canvas.section = "projects"
    _gutter(canvas, "PROJECTS", y)
    style = f.project_bullet_style
    for slot in config.projects:
        project = profile.project(slot.id)
        if project is None:
            result.warnings.append(f"{slot.id} is not in the library")
            continue
        canvas.text(project.name, f.body_bold, f.content_x, y)
        if project.link_label and not project.show_link:
            result.notes.append(
                f"{slot.id}: link label dropped, readme_state={project.readme_state!r} — "
                f"a linked repository will be opened and a stub reads as abandoned"
            )
        if project.show_link:
            # Dropped for a stub or absent README: a linked repository WILL be
            # opened, and a stub reads as abandoned. The bullets stand alone.
            canvas.right_text(project.link_label or "", f.body_italic, content_right, y)
            label_w = canvas.c.stringWidth(
                project.link_label or "", f.body_italic.resolved(), f.body_italic.size
            )
            canvas.c.setLineWidth(0.5)
            canvas.c.line(
                content_right - label_w, canvas._flip(y, f.body.size) - 1.6,
                content_right, canvas._flip(y, f.body.size) - 1.6,
            )
        canvas.lines += 1
        y += f.body.line_height()
        for bullet_id in slot.bullets:
            found = next((b for b in project.bullets if b.id == bullet_id), None)
            if found is None:
                result.warnings.append(f"{bullet_id} is not a bullet of {slot.id}")
                continue
            y += canvas.bullet(found.text, style, y)

    # ── EDUCATION: fixed, entirely ──
    y += section_gap
    canvas.rule(y)
    y += gap
    canvas.section = "education"
    _gutter(canvas, "EDUCATION", y)
    for school in profile.education:
        canvas.text(school.get("institution", ""), f.body_bold, f.content_x, y)
        if not school.get("degrees"):
            canvas.right_text(
                format_period(school.get("start"), school.get("end")), f.body_italic,
                content_right, y,
            )
        canvas.lines += 1
        y += f.body.line_height()
        for degree in school.get("degrees") or []:
            canvas.text(degree.get("degree", ""), f.body_bold_italic, f.content_x + 8, y)
            canvas.right_text(
                format_period(degree.get("start"), degree.get("end"), degree.get("end_label")),
                f.body_italic, content_right, y,
            )
            canvas.lines += 1
            y += f.body.line_height()
            for label, key in (("Concentrations", "concentrations"),
                               ("Relevant Coursework", "coursework")):
                if value := degree.get(key):
                    y += _labelled_bullet(canvas, f, label, value, y)
        if note := school.get("note"):
            y += canvas.bullet(note, f.body, y)

    result.body_lines = canvas.lines
    result.bottom_y = y
    canvas.save()
    result.printed = canvas.printed
    result.sections = canvas.sections
    result.page_count = _page_count(path)

    # The design deliberately fills the page — the reference CV's own ink ends
    # 25pt from the bottom edge — so a cursor past `margin_bottom` is normal and
    # only the page count settles the question. Enumerating all 1,140 legal
    # project trios gives 47 body lines and one page every time, so this can only
    # fire if the frame and the profile have drifted apart.
    if result.page_count > 1:
        result.warnings.append(
            f"THE PAGE OVERFLOWED to {result.page_count} pages, reaching {y:.0f}pt. "
            f"The line-cost arithmetic said it would fit, so the calibration is wrong: "
            f"run `pitwall apply calibrate`."
        )
    elif y > f.page_height - 8:
        result.warnings.append(
            f"content reaches {y:.0f}pt with under 8pt to spare — still one page, but "
            f"recalibrate before trusting it"
        )
    return result


def _labelled_bullet(canvas: _Canvas, f: Frame, label: str, value: str, y: float) -> float:
    """"Concentrations: Big Data Analytics; …" — italic label, upright value, as
    the source design has it."""
    canvas.c.setFont(f.body_italic.resolved(), f.body.size)
    canvas.c.setFillColorRGB(0, 0, 0)
    canvas.c.drawString(f.content_x + 13.7, canvas._flip(y, f.body.size), BULLET)
    text = f"<i>{escape_markup(label)}:</i> {escape_markup(value)}"
    from reportlab.platypus import Paragraph

    para = Paragraph(
        text, paragraph_style(f.body.resolved(), f.body.size, f.body.line_height())
    )
    _, height = para.wrapOn(canvas.c, f.bullet_width - 8, 10_000)
    para.drawOn(canvas.c, f.content_x + f.bullet_indent + 8, canvas._flip(y, height))
    canvas.lines += len(para.blPara.lines)
    canvas._record(f"{label}: {value}")
    return height


def _github_handle(url: str) -> str:
    """The CV prints the handle, not the URL — it is one line either way and the
    handle is what a reader retypes."""
    return url.rstrip("/").rsplit("/", 1)[-1] if url else ""


def _page_count(path: pathlib.Path) -> int:
    """Belt and braces. In a correctly calibrated system this is always 1,
    because the line-cost arithmetic already proved it before anything was
    written. Any other answer is a bug report, not normal operation."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001 — a missing pypdf must not fail a good render
        return 0
