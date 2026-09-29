"""SLOT A and SLOT B — the two things that vary, and the limits they respect.

The knapsack is solved exactly rather than greedily, so the obvious test is that
its answer really is the optimum. With 20 selectable projects the search space is
1,140 combinations, which is small enough to verify against exhaustive
enumeration rather than trusting the implementation.
"""

from __future__ import annotations

import itertools

import pytest

from pitwall.apply.families import FAMILIES, signal_for
from pitwall.apply.layout import Frame
from pitwall.apply.loader import load_profile
from pitwall.apply.selector import (
    JobSignal,
    build_config,
    diversity_bonus,
    pack_row,
    project_score,
    row_line_budget,
    select_projects,
    selectable,
)


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def frame():
    return Frame.load()


@pytest.fixture(params=sorted(FAMILIES))
def signal(request):
    return signal_for(request.param)


def test_conflicting_projects_are_never_selectable(profile):
    """Ophis's bullets already describe the market-structure and Monte Carlo
    work. Printing those projects too would show one body of work twice."""
    pool = {p.id for p in selectable(profile)}
    conflicted = {p.id for p in profile.projects if p.conflicts_with}
    assert conflicted, "the fixture has lost its conflicts_with records"
    assert not (pool & conflicted)


def test_selected_trio_is_the_true_optimum(profile, frame, signal):
    """Verified against exhaustive enumeration, not against itself."""
    slots, _ = select_projects(profile, frame, signal)
    chosen_score = sum(s.score for s in slots) + 0.4 * diversity_bonus(
        tuple(profile.project(s.id) for s in slots)
    )

    pool = selectable(profile)
    scores = {p.id: project_score(p, signal) for p in pool}
    best = max(
        sum(scores[p.id] for p in combo) + 0.4 * diversity_bonus(combo)
        for combo in itertools.combinations(pool, frame.project_count)
        if sum(frame.project_cost([b.text for b in p.bullets]) for p in combo)
        <= frame.project_budget
    )
    assert chosen_score == pytest.approx(best, abs=1e-6)


def test_trio_always_fits_the_line_budget(profile, frame, signal):
    slots, _ = select_projects(profile, frame, signal)
    assert len(slots) == frame.project_count
    assert sum(s.cost for s in slots) <= frame.project_budget


def test_trio_is_ordered_by_relevance(profile, frame, signal):
    slots, _ = select_projects(profile, frame, signal)
    assert [s.score for s in slots] == sorted((s.score for s in slots), reverse=True)


def test_rows_respect_both_budgets(profile, frame, signal):
    """The character budget is the declared one; the LINE budget is the one that
    decides the page. 193 characters of long method names renders as three lines
    in a two-line row, so both have to bind."""
    for row in frame.skill_rows:
        items = pack_row(profile, frame, row["id"], signal)
        joined = ", ".join(items)
        assert len(joined) <= frame.row_limit(row["id"]), row["id"]
        assert frame.line_cost(joined) <= row_line_budget(frame, row["id"]), row["id"]


def test_rows_never_contain_an_unprintable_skill(profile, frame, signal):
    printable = {s.name for s in profile.skills if s.printable}
    for row in frame.skill_rows:
        if profile.skill_rows.fixed_text(row["id"]) is not None:
            continue
        for name in pack_row(profile, frame, row["id"], signal):
            assert name in printable, name


def test_pinned_items_always_appear(profile, frame, signal):
    for row in frame.skill_rows:
        pinned = profile.skill_rows.pinned(row["id"])
        if not pinned:
            continue
        items = pack_row(profile, frame, row["id"], signal)
        for name in pinned:
            assert name in items, f"{name} missing from {row['id']}"


def test_spoken_row_is_copied_verbatim(profile, frame, signal):
    assert pack_row(profile, frame, "row.spoken", signal) == [
        profile.skill_rows.fixed_text("row.spoken")
    ]


def test_different_jobs_produce_different_cvs(profile, frame):
    """G2: two different jobs must produce materially different CVs from one
    profile. A frame that cannot do this is just a static document."""
    picks = {
        family: tuple(s.id for s in build_config(profile, frame, signal_for(family)).projects)
        for family in ("quantitative", "machine_learning", "data_engineering")
    }
    assert len(set(picks.values())) == 3, picks


def test_advisory_preference_cannot_break_the_budget(profile, frame):
    """A model may prefer any trio it likes; the solver still enforces the line
    budget and records the override."""
    signal = signal_for("data_science")
    costly = {p.id: 1.0 for p in selectable(profile)[:3]}
    slots, overrides = select_projects(profile, frame, signal, advisory=costly)
    assert sum(s.cost for s in slots) <= frame.project_budget
    assert len(slots) == frame.project_count
    assert isinstance(overrides, list)


def test_unknown_variant_falls_back_to_canonical(profile, frame):
    config = build_config(
        profile, frame, signal_for("data_science"),
        variants={"ophis.b1": "ophis.b1.does_not_exist"},
    )
    assert config.experience_variants["ophis.b1"] is None
    assert any("not a declared variant" in o for o in config.overrides)


def test_declared_variants_are_accepted(profile, frame):
    wanted = {b.id: b.variants[0].id for b in profile.experience_bullets if b.variants}
    config = build_config(profile, frame, signal_for("data_science"), variants=wanted)
    for bullet_id, variant_id in wanted.items():
        assert config.experience_variants[bullet_id] == variant_id


def test_empty_signal_still_produces_a_valid_config(profile, frame):
    """A posting the analyzer could make nothing of must still yield a CV."""
    config = build_config(profile, frame, JobSignal())
    assert len(config.projects) == frame.project_count
    assert all(config.skills[row["id"]] for row in frame.skill_rows)
