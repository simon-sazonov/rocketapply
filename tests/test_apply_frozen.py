"""The frame is frozen — proved across many jobs, not asserted once.

The claim this file defends: a recruiter at the same company seeing two of
Simon's applications sees the same document. The blast radius of any selection
decision, model-driven or not, is three slots — it cannot reorder sections, drop
education, invent a summary or restructure experience, because nothing in the
config schema addresses those.
"""

from __future__ import annotations

import itertools
import random

import pytest

from pitwall.apply.families import FAMILIES, signal_for
from pitwall.apply.layout import Frame, fonts_available
from pitwall.apply.loader import load_profile
from pitwall.apply.render_docx import assert_same_content, render_docx
from pitwall.apply.render_pdf import format_period, render_cv
from pitwall.apply.selector import (
    CvConfig,
    JobSignal,
    ProjectSlot,
    build_config,
    choose_variants,
    pack_row,
    selectable,
)

requires_fonts = pytest.mark.skipif(
    not fonts_available(), reason="assets/fonts is not populated — run `make fonts`"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def frame():
    return Frame.load()


@pytest.fixture(scope="module")
def renders(profile, frame, tmp_path_factory):
    """One CV per role family, plus a few random signals — 20 in total."""
    directory = tmp_path_factory.mktemp("frozen")
    random.seed(11)
    tags = sorted({t for p in profile.projects for b in p.bullets for t in b.tags})
    signals = [signal_for(f) for f in sorted(FAMILIES)]
    while len(signals) < 20:
        signals.append(
            JobSignal(
                required=set(random.sample(tags, 5)),
                preferred=set(random.sample(tags, 5)),
                role_family=random.choice(sorted(FAMILIES)),
            )
        )
    out = []
    for i, signal in enumerate(signals):
        # WITH VARIANTS, because that is what `generate_application` does. Until
        # 28 Sep 2026 the pipeline passed none and this fixture matched it; now
        # the shipping path swaps experience wording per role family, and the
        # frozen guarantees have to hold on the path that actually ships.
        config = build_config(
            profile, frame, signal, variants=choose_variants(profile, signal),
        )
        out.append((config, render_cv(profile, frame, config, directory / f"cv{i}.pdf")))
    return out


@requires_fonts
def test_every_render_is_one_page(renders):
    for _, result in renders:
        assert result.page_count == 1
        assert result.warnings == []


@requires_fonts
def test_body_line_count_is_invariant(renders):
    """Not merely "it fits" — the page is the SAME HEIGHT every time. That is
    what makes overflow impossible rather than unlikely."""
    assert len({r.body_lines for _, r in renders}) == 1


@requires_fonts
@pytest.mark.parametrize("section", ["header", "contact", "education"])
def test_fixed_sections_are_byte_identical(renders, section):
    first = renders[0][1].sections[section]
    for _, result in renders[1:]:
        assert result.sections[section] == first, section


@requires_fonts
def test_experience_keeps_its_shape_while_the_wording_may_swap(renders):
    """Experience is fixed in STRUCTURE, not in prose.

    All three entries and all seven bullets print on every application, in this
    order — that is the promise a recruiter seeing two applications relies on.
    What may change is the wording, and only to a variant the human pre-approved
    and which `_validated_variants` has proved line-neutral. Byte-identity was
    the wrong test for that: it passed only because the pipeline was not passing
    variants at all.
    """
    first = renders[0][1].sections["experience"]
    for _, result in renders[1:]:
        assert len(result.sections["experience"]) == len(first)
    # …and the swapping is real, or the variants are dead weight again.
    assert len({tuple(r.sections["experience"]) for _, r in renders}) > 1


@requires_fonts
def test_the_variable_sections_actually_vary(renders):
    """The other half of the claim. A frame nothing can change is not tailoring."""
    assert len({tuple(r.sections["projects"]) for _, r in renders}) > 1
    assert len({tuple(r.sections["skills"]) for _, r in renders}) > 1


@requires_fonts
def test_experience_prints_all_seven_bullets_as_approved_text(profile, renders):
    """Every bullet prints, every time — as its canonical text or as one of the
    variants a human pre-approved for it. Never as anything else, and never
    absent.

    This asserted the CANONICAL text until 28 Sep 2026, which was only true
    because the pipeline never selected a variant. The guarantee a recruiter
    relies on is that the same seven bullets are there, not that they are
    phrased identically for a quant role and an MLOps one.
    """
    for _, result in renders:
        printed = set(result.sections["experience"])
        for bullet in profile.experience_bullets:
            approved = {bullet.text, *(v.text for v in bullet.variants)}
            assert approved & printed, bullet.id


@requires_fonts
def test_education_is_never_touched(profile, renders):
    for _, result in renders:
        printed = set(result.sections["education"])
        for entry in profile.education:
            assert entry["institution"] in printed
            for degree in entry.get("degrees") or []:
                assert degree["degree"] in printed


@requires_fonts
def test_every_printed_string_traces_to_the_profile(profile, renders, frame):
    """§10.4. The CV path carries a full guarantee, not a probabilistic one:
    every string on the page is byte-identical to a bullet, an approved variant,
    a metric claim or a profile field."""
    allowed = {
        "CONTACT", "SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION",
        profile.personal.name, profile.personal.title, profile.personal.email,
        profile.personal.linkedin, profile.personal.github.rstrip("/").rsplit("/", 1)[-1],
        profile.skill_rows.fixed_text("row.spoken"),
    }
    for bullet in profile.experience_bullets:
        allowed.add(bullet.text)
        allowed.update(v.text for v in bullet.variants)
    for entry in profile.experience:
        allowed.add(f"{entry.org} – {entry.role}")
        allowed.add(f"  |{entry.employment_type}|")
        allowed.add(format_period(entry.start, entry.end))
    for project in profile.projects:
        allowed.add(project.name)
        allowed.add(project.link_label or "")
        allowed.update(b.text for b in project.bullets)
    for entry in profile.education:
        allowed.add(entry.get("institution", ""))
        allowed.add(entry.get("note", ""))
        allowed.add(format_period(entry.get("start"), entry.get("end")))
        for degree in entry.get("degrees") or []:
            allowed.add(degree.get("degree", ""))
            allowed.add(format_period(degree.get("start"), degree.get("end"), degree.get("end_label")))
            for label, key in (("Concentrations", "concentrations"),
                               ("Relevant Coursework", "coursework")):
                if degree.get(key):
                    allowed.add(f"{label}: {degree[key]}")

    for config, result in renders:
        packed = {", ".join(v) for v in config.skills.values()}
        for value in result.printed:
            assert value in allowed or value in packed, f"untraceable string: {value!r}"


@requires_fonts
def test_pdf_and_docx_never_diverge(profile, frame, tmp_path):
    """A divergence means one document was built from something other than the
    profile ids, which is a bug rather than a formatting difference."""
    for family in sorted(FAMILIES):
        config = build_config(profile, frame, signal_for(family))
        pdf = render_cv(profile, frame, config, tmp_path / f"{family}.pdf")
        docx = render_docx(profile, config, tmp_path / f"{family}.docx")
        assert assert_same_content(pdf.printed, docx.bullets) == [], family


@requires_fonts
def test_no_legal_project_trio_can_overflow(profile, frame):
    """Exhaustive over the whole of SLOT A — every combination the selector may
    legally return, not just the ones a signal happens to pick. This is
    arithmetic rather than rendering, so it is fast enough to run every time."""
    pool = selectable(profile)
    budget = frame.project_budget
    worst = max(
        sum(frame.project_cost([b.text for b in p.bullets]) for p in combo)
        for combo in itertools.combinations(pool, frame.project_count)
    )
    assert worst <= budget, f"worst trio costs {worst} against a {budget}-line budget"


@requires_fonts
def test_a_deliberately_adversarial_config_still_fits(profile, frame, tmp_path):
    """The three most expensive projects, every variant swapped in, and skill
    rows packed against a signal that favours the longest names."""
    pool = sorted(
        selectable(profile),
        key=lambda p: -sum(len(b.text) for b in p.bullets),
    )[: frame.project_count]
    signal = JobSignal(required={"machine_learning", "data_engineering"}, role_family="data_science")
    config = CvConfig(
        projects=[ProjectSlot(id=p.id, bullets=[b.id for b in p.bullets]) for p in pool],
        skills={r["id"]: pack_row(profile, frame, r["id"], signal) for r in frame.skill_rows},
        experience_variants={
            b.id: (b.variants[-1].id if b.variants else None) for b in profile.experience_bullets
        },
    )
    result = render_cv(profile, frame, config, tmp_path / "adversarial.pdf")
    assert result.page_count == 1
    assert result.warnings == []
