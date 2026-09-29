"""The ATS variant — same content, plainer structure.

The designed PDF is the primary submission. This exists because the source CV is
assembled from independent text frames, so its text extracts in non-linear order:
position-aware parsers cope, weaker ones attach a date to the wrong entry. This
carries identical content from identical ids; only presentation differs.

The rules it follows (§8.8a), and the reason for each:

* single column, linear reading order — no text boxes, no layout tables
* the gutter labels become real Word heading styles, not bold body text
* standard fonts, because a parser may not have the vendored ones
* dates inline with the entry, never in a side rail
* contact details in the BODY, never in a header — many parsers drop headers
* skills as comma-separated text, never a table or a rating graphic

`assert_same_content` proves the two outputs agree. A divergence means one of
them was built from something other than the profile ids, which is a bug.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from .models import Profile
from .render_pdf import format_period
from .selector import CvConfig

__all__ = ["DocxResult", "render_docx", "assert_same_content"]

#: Widely installed and reliably parsed. Not the design faces — a parser that
#: does not have DM Sans may substitute badly or drop the run.
ATS_FONT = "Calibri"
ATS_SIZE = 10


@dataclass
class DocxResult:
    path: pathlib.Path
    #: Every bullet string written, for the cross-check against the PDF.
    bullets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _style(document) -> None:
    from docx.shared import Pt

    normal = document.styles["Normal"]
    normal.font.name = ATS_FONT
    normal.font.size = Pt(ATS_SIZE)
    for section in document.sections:
        section.left_margin = section.right_margin = Pt(50)
        section.top_margin = section.bottom_margin = Pt(40)


def render_docx(
    profile: Profile, config: CvConfig, path: pathlib.Path | str
) -> DocxResult:
    from docx import Document
    from docx.shared import Pt

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    _style(document)
    result = DocxResult(path=path)

    def heading(text: str, level: int) -> None:
        document.add_heading(text, level=level)

    def line(text: str, *, bold: bool = False) -> None:
        para = document.add_paragraph()
        run = para.add_run(text)
        run.bold = bold
        para.paragraph_format.space_after = Pt(2)

    def bullet(text: str) -> None:
        para = document.add_paragraph(text, style="List Bullet")
        para.paragraph_format.space_after = Pt(2)
        result.bullets.append(text)

    # ── header and contact, in the body ──
    heading(f"{profile.personal.name} — {profile.personal.title}", 0)
    contact = " | ".join(
        v for v in (profile.personal.email, profile.personal.github, profile.personal.linkedin) if v
    )
    line(contact)

    # ── skills ──
    heading("Skills", 1)
    for row in config.skills.values():
        if row:
            bullet(", ".join(row))

    # ── experience ──
    heading("Experience", 1)
    for entry in profile.experience:
        marker = f" ({entry.employment_type})" if entry.employment_type else ""
        line(
            f"{entry.org} — {entry.role}{marker}, "
            f"{format_period(entry.start, entry.end)}",
            bold=True,
        )
        for b in entry.bullets:
            chosen = config.experience_variants.get(b.id)
            variant = b.variant(chosen) if chosen else None
            bullet(variant.text if variant else b.text)

    # ── projects ──
    heading("Projects", 1)
    for slot in config.projects:
        project = profile.project(slot.id)
        if project is None:
            continue
        # The same rule as the PDF: a stub repository's link is what hurts.
        suffix = f" — {project.url}" if project.show_link and project.url else ""
        line(f"{project.name}{suffix}", bold=True)
        for bullet_id in slot.bullets:
            found = next((b for b in project.bullets if b.id == bullet_id), None)
            if found:
                bullet(found.text)

    # ── education ──
    heading("Education", 1)
    for school in profile.education:
        line(school.get("institution", ""), bold=True)
        for degree in school.get("degrees") or []:
            line(
                f"{degree.get('degree', '')}, "
                f"{format_period(degree.get('start'), degree.get('end'), degree.get('end_label'))}"
            )
            for label, key in (("Concentrations", "concentrations"),
                               ("Relevant Coursework", "coursework")):
                if value := degree.get(key):
                    bullet(f"{label}: {value}")
        if not school.get("degrees"):
            line(format_period(school.get("start"), school.get("end")))
        if note := school.get("note"):
            bullet(note)

    document.save(str(path))
    return result


def assert_same_content(pdf_printed: list[str], docx_bullets: list[str]) -> list[str]:
    """Every bullet in the DOCX must appear verbatim in the PDF.

    Returns the divergences. A non-empty result means one document was built
    from something other than the profile ids — which is the bug this check
    exists to catch, not a formatting difference to tolerate.
    """
    printed = set(pdf_printed)
    return [b for b in docx_bullets if b not in printed]
