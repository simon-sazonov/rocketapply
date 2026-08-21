"""Template rendering, alias fan-out, dedup, budget cap, operator degradation (§12.1)."""

from __future__ import annotations

import pytest

from pitwall.models import Company, Role, make_query_hash
from pitwall.query_builder import (
    Template,
    build,
    build_all,
    filter_templates,
    load_templates,
    priority_for,
)


def test_templates_load_and_validate(templates):
    assert templates
    ids = {t.id for t in templates}
    assert {"profile_exact", "plain_fallback", "plain_site_soft"} <= ids
    assert all(t.pattern for t in templates)


def test_unknown_for_each_field_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "templates:\n  - id: x\n    pattern: '{company}'\n    for_each: nonsense\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="for_each"):
        load_templates(bad)


def test_worked_example_from_section_6_3(company, role, templates, settings):
    """§6.3: the operator path renders the plan's ten listed queries."""
    specs = build(
        [company], [role], templates, settings,
        provider="serper", supports_operators=True, max_queries=0,
    )
    rendered = {s.query_text for s in specs}
    for expected in [
        'site:linkedin.com/in "McLaren Racing" "Data Scientist"',
        'site:linkedin.com/in "McLaren F1" "Data Scientist"',
        'site:linkedin.com/in "McLaren Applied" "Data Scientist"',
        'site:linkedin.com/in "McLaren Racing" "Data Science"',
        'site:linkedin.com/in "McLaren Racing" "ML Engineer"',
        'site:linkedin.com/in "McLaren Racing" "graduate Data Scientist"',
        'site:linkedin.com/in "McLaren Racing" "junior Data Scientist"',
        'site:linkedin.com/posts "McLaren Racing" "Data Scientist"',
        'site:github.com "McLaren Racing" "Data Scientist"',
    ]:
        assert expected in rendered, expected


def test_operator_degradation_drops_operator_templates(company, role, templates, settings):
    degraded = build(
        [company], [role], templates, settings,
        provider="ddgs", supports_operators=False, max_queries=0,
    )
    assert degraded
    assert all('"' not in s.query_text for s in degraded)
    assert not any(s.template_id == "profile_exact" for s in degraded)
    # ...and the free path still produces the plan's plain forms
    rendered = {s.query_text for s in degraded}
    assert "McLaren Racing Data Scientist linkedin profile" in rendered
    assert "site:linkedin.com/in McLaren Racing Data Scientist" in rendered


def test_family_restricted_templates_only_fire_for_their_families(company, templates, settings):
    strategy = Role(title="Race Strategist", family="strategy", aliases="", seniority="", keywords="")
    specs = build([company], [strategy], templates, settings,
                  provider="serper", supports_operators=True, max_queries=0)
    assert not any(s.template_id == "github_discovery" for s in specs)


def test_duplicate_query_strings_collapse_to_one(settings, templates):
    a = Company(name="AF Corse", aliases="AF Corse", negative_keywords="")  # alias == name
    r = Role(title="Data Engineer", aliases="Data Engineer", family="data_engineering",
             seniority="", keywords="")
    specs = build([a], [r], templates, settings, provider="ddgs",
                  supports_operators=False, max_queries=0)
    texts = [s.query_text for s in specs]
    assert len(texts) == len(set(texts))
    hashes = [s.query_hash for s in specs]
    assert len(hashes) == len(set(hashes))


def test_budget_cap_takes_the_top_n_by_priority(company, role, templates, settings):
    specs = build([company], [role], templates, settings,
                  provider="serper", supports_operators=True, max_queries=3)
    assert len(specs) == 3
    priorities = [s.priority for s in specs]
    assert priorities == sorted(priorities, reverse=True)


def test_priority_formula_matches_the_plan(settings):
    """`3·(4 − tier) + 2·(4 − role priority) + template weight`."""
    c = Company(name="X", tier=1, aliases="", negative_keywords="")
    r = Role(title="Y", family="strategy", priority=1, aliases="", seniority="", keywords="")
    t = Template(id="t", pattern="{company} {role}", weight=5)
    assert priority_for(c, r, t, settings) == pytest.approx(3 * 3 + 2 * 3 + 5)

    c3 = Company(name="X", tier=3, aliases="", negative_keywords="")
    r3 = Role(title="Y", family="strategy", priority=3, aliases="", seniority="", keywords="")
    assert priority_for(c3, r3, t, settings) == pytest.approx(3 * 1 + 2 * 1 + 5)


def test_inactive_rows_are_skipped(company, role, templates, settings):
    company.active = False
    assert build([company], [role], templates, settings, max_queries=0) == []
    company.active, role.active = True, False
    assert build([company], [role], templates, settings, max_queries=0) == []


def test_templates_needing_an_empty_placeholder_are_skipped(templates, settings):
    """A company with no aliases must not render `site:... "" "role"`."""
    c = Company(name="Solo", aliases="", negative_keywords="")
    r = Role(title="Data Scientist", family="data_science", aliases="", seniority="", keywords="")
    specs = build([c], [r], templates, settings, provider="serper",
                  supports_operators=True, max_queries=0)
    assert not any(s.template_id in {"profile_alias", "profile_role_alias", "profile_seniority"}
                   for s in specs)
    assert all('""' not in s.query_text for s in specs)


def test_query_hash_is_provider_scoped_and_whitespace_insensitive():
    assert make_query_hash("ddgs", "  foo   BAR ") == make_query_hash("ddgs", "foo bar")
    assert make_query_hash("ddgs", "foo bar") != make_query_hash("serper", "foo bar")


def test_build_all_reports_the_uncapped_total(company, role, settings):
    specs, summary = build_all([company], [role], settings, provider="serper",
                               supports_operators=True, max_queries=4)
    assert len(specs) == 4
    assert summary["capped_to"] == 4
    assert summary["planned"] > 4
    assert sum(summary["by_template"].values()) == 4


def test_explicit_template_allow_list(company, role, templates, settings):
    only = filter_templates(templates, supports_operators=True, only_ids=["profile_exact"])
    assert [t.id for t in only] == ["profile_exact"]
    specs = build([company], [role], templates, settings, provider="serper",
                  supports_operators=True, template_ids=["profile_exact"], max_queries=0)
    assert {s.template_id for s in specs} == {"profile_exact"}
