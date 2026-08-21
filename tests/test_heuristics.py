"""Every rule in §9.1, plus the McLaren Health Care and ex-Ferrari traps (§12.1)."""

from __future__ import annotations

import pytest

from pitwall.models import Company
from pitwall.scoring import heuristics as H
from pitwall.scoring.heuristics import passes, score_candidate

PROFILE = "https://www.linkedin.com/in/test-person"


def cand(**kw):
    base = {
        "profile_url": PROFILE,
        "full_name": "Test Person",
        "current_title": None,
        "current_company": None,
        "location": None,
        "headline": "",
        "evidence_count": 1,
    }
    base.update(kw)
    return base


def score(title="", snippet="", company=None, role=None, **kw):
    return score_candidate(cand(**kw), company, role, serp_title=title, serp_snippet=snippet)


# ───────────────────────────── positive rules ────────────────────────────


def test_company_canonical_name_in_title(company, role):
    r = score("Test Person - Analyst - McLaren Racing", company=company)
    assert r.score == pytest.approx(H.W_COMPANY_TITLE)


def test_company_alias_in_title_scores_less_than_the_canonical_name(company):
    r = score("Test Person - Analyst - McLaren Applied", company=company)
    assert r.score == pytest.approx(H.W_COMPANY_ALIAS_TITLE)
    assert H.W_COMPANY_ALIAS_TITLE < H.W_COMPANY_TITLE


def test_company_in_snippet_only_is_weaker_still(company):
    r = score("Test Person - Analyst - Somewhere", "Builds tooling for McLaren Racing",
              company=company)
    assert r.score == pytest.approx(H.W_COMPANY_SNIPPET)
    assert any("snippet only" in reason for reason in r.reasons)
    assert H.W_COMPANY_SNIPPET < H.W_COMPANY_ALIAS_TITLE < H.W_COMPANY_TITLE


def test_role_exact_alias_family_and_snippet_form_a_descending_ladder(role):
    exact = score("Test Person - Data Scientist - X", role=role).score
    alias = score("Test Person - ML Engineer - X", role=role).score
    family = score("Test Person - Applied Scientist - X", role=role).score
    snippet_only = score("Test Person - Chef - X", "does data science work", role=role).score
    assert exact == pytest.approx(H.W_ROLE_TITLE)
    assert alias == pytest.approx(H.W_ROLE_ALIAS_TITLE)
    assert family == pytest.approx(H.W_FAMILY_TITLE)
    assert snippet_only == pytest.approx(H.W_ROLE_SNIPPET)
    assert exact > alias > family > snippet_only


def test_wanted_seniority_adds_points(role):
    with_seniority = score("Test Person - graduate Data Scientist - X", role=role).score
    without = score("Test Person - Data Scientist - X", role=role).score
    assert with_seniority - without == pytest.approx(H.W_SENIORITY)


def test_role_keywords_cap_at_twelve(role):
    r = score("Test Person - Chef - X", "Monte Carlo and tyre degradation modelling", role=role)
    keyword_points = next(x for x in r.reasons if "role keyword" in x)
    assert "+12" in keyword_points


def test_location_matching_the_company_country(company):
    r = score("Test Person - X - Y", company=company, location="Woking, England, United Kingdom")
    assert r.score == pytest.approx(H.W_LOCATION)


def test_evidence_bonus_applies_from_three_distinct_queries(company):
    two = score("Test Person - X - Y", company=company, evidence_count=2).score
    three = score("Test Person - X - Y", company=company, evidence_count=3).score
    assert three - two == pytest.approx(H.W_EVIDENCE)


# ───────────────────────────── negative rules ────────────────────────────


def test_mclaren_health_care_trap_is_killed(company, role):
    r = score(
        "David Brooks - Data Analyst - McLaren Health Care",
        "Data Analyst at McLaren Health Care, Macomb, Michigan",
        company=company, role=role,
    )
    assert r.score == 0.0
    assert any("negative keyword" in reason for reason in r.reasons)
    assert not passes(r, 25)


def test_ex_ferrari_trap_is_killed(company, role):
    r = score(
        "Luca Rossi - Strategy Consultant",
        "Ex-McLaren Racing data scientist, now at Bain",
        company=company, role=role,
    )
    assert not passes(r, 25)
    assert any("past-tense" in reason for reason in r.reasons)


def test_recruiters_are_penalised_unless_you_want_them(company):
    r = score("Kim Lee - Talent Acquisition Partner - McLaren Racing", company=company)
    assert any("recruiter" in reason for reason in r.reasons)
    allowed = score_candidate(
        cand(), company, None,
        serp_title="Kim Lee - Talent Acquisition Partner - McLaren Racing",
        serp_snippet="", allow_recruiters=True,
    )
    assert allowed.score > r.score


def test_students_without_an_employer_are_penalised_but_kept(company):
    r = score("Ana Mota - Student at TU Delft", "MSc candidate", company=company)
    assert any("student" in reason for reason in r.reasons)
    with_employer = score_candidate(
        cand(current_company="McLaren Racing"), company, None,
        serp_title="Ana Mota - Student at TU Delft", serp_snippet="MSc candidate",
    )
    assert not any("student" in reason for reason in with_employer.reasons)


def test_non_profile_urls_are_hard_rejected(company, role):
    r = score_candidate(
        cand(profile_url="https://www.linkedin.com/posts/x_activity-1"), company, role
    )
    assert r.hard_reject is True
    assert r.score == 0.0
    assert not passes(r, 0)


@pytest.mark.parametrize(
    "text",
    ["next generation", "Experienced engineer", "index of alexandria", "Alex Smith", "Texas"],
)
def test_the_ex_marker_does_not_fire_on_ordinary_words(text, company):
    r = score(f"Test Person - {text} - McLaren Racing", company=company)
    assert not any("past-tense" in reason for reason in r.reasons)


@pytest.mark.parametrize(
    "text",
    ["Ex-McLaren engineer", "ex McLaren engineer", "Formerly at McLaren", "there until 2023",
     "McLaren alum"],
)
def test_the_past_tense_markers_do_fire_when_they_should(text, company):
    r = score("Test Person - Engineer - Somewhere", text, company=company)
    assert any("past-tense" in reason for reason in r.reasons), text


# ──────────────────────────── composition ────────────────────────────────


def test_scores_are_clamped_to_the_zero_hundred_range(company, role):
    perfect = score(
        "Jane Doe - graduate Data Scientist - McLaren Racing",
        "Monte Carlo and tyre degradation work at McLaren Racing",
        company=company, role=role, location="Woking, England, United Kingdom",
        evidence_count=9,
    )
    assert 0.0 <= perfect.score <= 100.0
    assert perfect.score >= 75

    awful = score(
        "X - Recruiter - McLaren Health Care", "ex- hospital Macomb", company=company, role=role
    )
    assert awful.score == 0.0


def test_sponsor_prefixes_do_not_break_company_matching():
    ferrari = Company(name="Ferrari", aliases="Scuderia Ferrari", negative_keywords="")
    r = score("Luca Rossi - Strategy Engineer - Scuderia Ferrari HP", company=ferrari)
    assert r.score >= H.W_COMPANY_ALIAS_TITLE


def test_every_result_carries_a_human_readable_reason(company, role):
    for title in ("Jane - Data Scientist - McLaren Racing", "Nobody - Nothing - Nowhere"):
        r = score(title, company=company, role=role)
        assert r.reasons and all(isinstance(x, str) and x for x in r.reasons)


def test_global_markers_can_be_overridden_by_config(company):
    r = score_candidate(
        cand(), company, None,
        serp_title="Test Person - Trainer - McLaren Racing",
        serp_snippet="horse racing tipster",
        markers={"global": ["horse racing"]},
    )
    assert r.score == 0.0
    assert any("horse racing" in reason for reason in r.reasons)


def test_passes_respects_the_threshold(company, role):
    r = score("Jane - Data Scientist - McLaren Racing", company=company, role=role)
    assert passes(r, 25) is True
    assert passes(r, 99) is False
