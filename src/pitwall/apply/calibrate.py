"""Re-measure the frame, and say where the numbers disagree.

Three questions, because there are three ways the page can quietly stop fitting:

1. Does the MEASURED line count match the `rendered_lines` declared in
   `layout.yaml`? Those were read off the reference PDF by eye.
2. Does the measured count match the CHARACTER-WRAP count the validator uses?
   The validator is what runs in CI, so if the two drift apart the guarantee is
   only as good as whichever one you happened to run.
3. How much slack is left in the geometry? The (size, width) pair was chosen from
   a wide band of solutions. A new bullet can narrow that band, and a band about
   to close is the warning that arrives before a two-page CV does.

Exits non-zero on any disagreement. That is the point: a surprise here is cheap,
the same surprise after an employer opens the file is not.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from . import PROFILE_DIR
from .layout import Frame
from .validate import load_yaml, wrap_lines

__all__ = ["CalibrationReport", "calibrate"]


@dataclass
class Drift:
    bullet_id: str
    text: str
    measured: int
    other: int
    kind: str  # "declared" | "charwrap"

    def __str__(self) -> str:
        against = "declared in layout.yaml" if self.kind == "declared" else "character wrap"
        return (
            f"{self.bullet_id}: measured {self.measured} line(s), "
            f"{against} says {self.other} ({len(self.text)} chars)"
        )


@dataclass
class CalibrationReport:
    measured: bool = False
    drifts: list[Drift] = field(default_factory=list)
    over_budget: list[str] = field(default_factory=list)
    #: How far the bullet column may narrow before a bullet GAINS a line.
    #: This is the direction that breaks the page.
    slack_narrow: float | None = None
    #: How far it may widen before a bullet LOSES one. Cosmetic: a gap, not an
    #: overflow.
    slack_wide: float | None = None
    band_widths: tuple[float, float] | None = None
    experience_lines: int = 0
    worst_trio: int = 0
    project_costs: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.drifts and not self.over_budget


def _bullets(directory: pathlib.Path) -> tuple[list[tuple[str, str, int | None]], list[dict]]:
    """Experience bullets with their declared line counts, and the raw projects."""
    exp = load_yaml(directory, "experience.yaml") or []
    projects = load_yaml(directory, "projects.yaml") or []
    rows = [
        (b.get("id", "?"), b.get("text", ""), b.get("rendered_lines"))
        for entry in exp
        for b in entry.get("bullets") or []
    ]
    return rows, projects


def _band(
    frame: Frame,
    exp_rows: list[tuple[str, str, int | None]],
    project_texts: list[str],
) -> tuple[float | None, float | None, tuple[float, float] | None]:
    """How far the column can move before some bullet changes line count.

    DIRECTION MATTERS, and the two directions are not equally serious:

    * NARROWER and a bullet gains a line. That is the dangerous one — the page
      grows and the frame breaks.
    * WIDER and a bullet loses one. Cosmetic: the block shrinks and leaves a gap,
      but nothing overflows.

    So this reports both, and sweeps at the live font size using the same
    paragraph engine as `line_cost` — measuring the frame two different ways
    would defeat the point of measuring it at all.
    """
    if not frame.measured:
        return None, None, None

    declared = [(t, n) for _, t, n in exp_rows if n is not None]
    style = frame.project_bullet_style

    def holds(width: float) -> bool:
        return all(frame.line_cost(t, width=width) == n for t, n in declared) and all(
            frame.line_cost(t, style, width=width) == 1 for t in project_texts
        )

    if not holds(frame.bullet_width):
        return None, None, None

    live = frame.bullet_width
    narrow = wide = live
    step = 0.5
    while narrow - step > 200 and holds(narrow - step):
        narrow -= step
    while wide + step < 560 and holds(wide + step):
        wide += step
    return live - narrow, wide - live, (narrow, wide)


def calibrate(directory: pathlib.Path | str | None = None) -> CalibrationReport:
    directory = pathlib.Path(directory) if directory else PROFILE_DIR
    frame = Frame.load(directory)
    report = CalibrationReport(measured=frame.measured)

    if not frame.measured:
        report.notes.append(
            "fonts are not installed, so this run compares character wrap against itself "
            "and proves nothing about the rendered page. Run `make fonts`."
        )

    exp_rows, projects = _bullets(directory)

    # ── 1 & 2: experience bullets, against both other answers ──
    total = 0
    for bid, text, declared in exp_rows:
        measured = frame.line_cost(text)
        total += measured
        if declared is not None and measured != declared:
            report.drifts.append(Drift(bid, text, measured, declared, "declared"))
        char = wrap_lines(text, frame.wrap_width_safe)
        if measured != char:
            report.drifts.append(Drift(bid, text, measured, char, "charwrap"))
    report.experience_lines = total
    if total > frame.experience_budget:
        report.over_budget.append(
            f"experience: {total} lines against a {frame.experience_budget}-line budget"
        )

    # Variants must be line-neutral under MEASUREMENT too, not only under the
    # character wrap the validator uses.
    for entry in load_yaml(directory, "experience.yaml") or []:
        for bullet in entry.get("bullets") or []:
            canonical = frame.line_cost(bullet.get("text", ""))
            for variant in bullet.get("variants") or []:
                vcost = frame.line_cost(variant.get("text", ""))
                if vcost != canonical:
                    report.drifts.append(
                        Drift(variant.get("id", "?"), variant.get("text", ""),
                              vcost, canonical, "declared")
                    )

    # ── projects ──
    project_texts: list[str] = []
    style = frame.project_bullet_style
    for project in projects:
        texts = [b.get("text", "") for b in project.get("bullets") or []]
        project_texts.extend(texts)
        cost = 1 + sum(frame.line_cost(t, style) for t in texts)
        report.project_costs[project.get("id", "?")] = cost
        for bid, text in ((b.get("id", "?"), b.get("text", "")) for b in project.get("bullets") or []):
            measured = frame.line_cost(text, style)
            char = wrap_lines(text, frame.wrap_width_safe)
            if measured != char:
                report.drifts.append(Drift(bid, text, measured, char, "charwrap"))

    worst = sorted(report.project_costs.values(), reverse=True)[: frame.project_count]
    report.worst_trio = sum(worst)
    if report.worst_trio > frame.project_budget:
        report.over_budget.append(
            f"projects: the worst trio the selector may legally pick costs "
            f"{report.worst_trio} lines against a {frame.project_budget}-line budget"
        )

    # ── 3: how much room is left ──
    report.slack_narrow, report.slack_wide, report.band_widths = _band(
        frame, exp_rows, project_texts
    )
    if report.band_widths is None and frame.measured:
        report.over_budget.append(
            "the live geometry does not reproduce every declared line count — "
            "the frame and the profile have drifted apart"
        )
    return report
