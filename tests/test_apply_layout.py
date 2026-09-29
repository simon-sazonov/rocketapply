"""The frame, and the line-cost model that makes fitting a guarantee.

These are the checks that catch a profile edit before it reaches an employer:
a bullet one character too long, a variant that wraps differently from the
bullet it replaces, a skills row that renders taller than it declares.
"""

from __future__ import annotations

import pytest

from pitwall.apply.calibrate import calibrate
from pitwall.apply.layout import Frame, escape_markup, fonts_available
from pitwall.apply.loader import load_profile
from pitwall.apply.validate import MAX_PROJECT_BULLET, MAX_TITLE_LINE, validate

requires_fonts = pytest.mark.skipif(
    not fonts_available(), reason="assets/fonts is not populated — run `make fonts`"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def frame():
    return Frame.load()


def test_profile_validates_clean():
    """Nothing downstream is trustworthy without this. An unknown tag matches
    nothing, which looks exactly like having no experience."""
    report = validate()
    assert report.errors == []
    assert report.ok


def test_experience_shape_is_fixed(profile):
    assert [(e.id, len(e.bullets)) for e in profile.experience] == [
        ("ophis", 3), ("freedomcode", 3), ("beesurfing", 1)
    ]


@requires_fonts
def test_measured_line_cost_matches_declared(profile, frame):
    """`rendered_lines` was read off the reference PDF by eye. This is the
    machine's opinion, and the two have to agree."""
    for bullet in profile.experience_bullets:
        if bullet.rendered_lines is not None:
            assert frame.line_cost(bullet.text) == bullet.rendered_lines, bullet.id


@requires_fonts
def test_variants_are_line_neutral(profile, frame):
    """A rephrasing that changes how many lines a bullet occupies breaks the
    frame. Checked here under MEASUREMENT, where the validator checks it under
    the character wrap — both have to hold."""
    for bullet in profile.experience_bullets:
        canonical = frame.line_cost(bullet.text)
        for variant in bullet.variants:
            assert frame.line_cost(variant.text) == canonical, variant.id


@requires_fonts
def test_every_project_costs_three_lines(profile, frame):
    """1 title + 2 single-line bullets. Any project costing more removes
    combinations from the pool the selector can choose from."""
    for project in profile.projects:
        cost = frame.project_cost([b.text for b in project.bullets])
        assert cost == 3, f"{project.id} costs {cost}"


def test_project_bullets_respect_the_character_limits(profile):
    for project in profile.projects:
        title = f"{project.name} {project.link_label or ''}".strip()
        assert len(title) <= MAX_TITLE_LINE, project.id
        assert len(project.bullets) == 2, project.id
        for bullet in project.bullets:
            assert len(bullet.text) <= MAX_PROJECT_BULLET, bullet.id


def test_worst_case_trio_fits_the_budget(profile, frame):
    costs = sorted(
        (frame.project_cost([b.text for b in p.bullets]) for p in profile.projects), reverse=True
    )
    assert sum(costs[:frame.project_count]) <= frame.project_budget


@requires_fonts
def test_calibration_reports_no_drift():
    report = calibrate()
    assert report.drifts == [], [str(d) for d in report.drifts]
    assert report.over_budget == []


@requires_fonts
def test_geometry_has_room_in_the_dangerous_direction():
    """Narrower means a bullet GAINS a line and the page breaks. Wider only
    costs a cosmetic gap, so only the narrow direction needs real margin."""
    report = calibrate()
    assert report.slack_narrow is not None
    assert report.slack_narrow >= 5.0, f"only {report.slack_narrow}pt of margin"


def test_ampersands_survive_the_paragraph_parser(frame):
    """Six strings in the profile carry an ampersand — "Data Pipelines & ETL".
    Paragraph treats its input as markup, so an unescaped one raises mid-render."""
    assert escape_markup("Data Pipelines & ETL") == "Data Pipelines &amp; ETL"
    assert frame.line_cost("Data Pipelines & ETL") == 1


def test_row_spoken_is_fixed_text_with_one_character_of_slack(profile, frame):
    text = profile.skill_rows.fixed_text("row.spoken")
    assert text is not None
    assert len(text) <= frame.row_limit("row.spoken")
