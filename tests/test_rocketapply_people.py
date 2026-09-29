"""People at the employers you applied to — found by PitWall's engine, kept out of PitWall.

The two promises: everyone found lands in the employer database and none of
it in the main one (PitWall's People page reads `candidates` unfiltered), and
each person is grouped by the search that found them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pitwall.jobsearch import people as people_mod
from pitwall.jobsearch.people import (
    GROUPS,
    alumni_roles,
    employer_settings,
    find_people,
    known_in_pitwall,
    people_at,
    roles_for_job,
    short_title,
)
from pitwall.search.base import SearchHit

PROFILE = SimpleNamespace(
    education=[{"institution": "Nova University of Lisbon | Information Management School"},
               {"institution": "Dnipro Scientific Lyceum of Information Technologies"}],
    experience=[SimpleNamespace(org="Ophis Research")],
)


def _hit(rank, slug, name, title, company="Anthropic"):
    return SearchHit(rank, f"https://www.linkedin.com/in/{slug}",
                     f"{name} - {title} - {company} | LinkedIn",
                     f"{title} at {company}. London.", {})


class ByQuery:
    """Answers each search by what it is looking for."""

    name = "ddgs"

    def __init__(self):
        self.queries: list[str] = []

    def supports_operators(self):
        return True

    def remaining_quota(self):
        return None

    def search(self, query, *, max_results=20, region="wt-wt"):
        self.queries.append(query)
        if "Recruiter" in query or "Talent" in query:
            return [_hit(1, "rita-recruits", "Rita Recruits", "Technical Recruiter")]
        if "Research Engineer" in query:
            return [_hit(1, "pete-peer", "Pete Peer", "Research Engineer")]
        if "Nova University" in query:
            return [_hit(1, "ana-alumna", "Ana Alumna", "Member of Technical Staff")]
        return []


@pytest.fixture()
def stub_search(monkeypatch):
    provider = ByQuery()
    monkeypatch.setattr("pitwall.pipeline.build_chain", lambda *a, **k: [provider])
    monkeypatch.setattr("pitwall.query_builder.build_chain", lambda *a, **k: [provider],
                        raising=False)
    return provider


def test_a_job_title_is_cut_to_what_peers_are_called():
    assert short_title("Research Engineer, Code RL (Reinforcement Learning)") == \
        "Research Engineer"
    assert short_title("Graduate Machine Learning Engineer (m/w/d)") == \
        "Machine Learning Engineer"
    assert short_title("Senior ML Engineer - Perception") == "ML Engineer"


def test_alumni_are_schools_and_past_employers_not_high_schools():
    rows = alumni_roles(PROFILE)
    assert [r.title for r in rows] == ["Nova University of Lisbon", "Ophis Research"]
    assert rows[0].aliases == ["Information Management School"]
    assert {r.group for r in rows} == {"alumni"}


def test_every_group_gets_rows_and_titles_are_unique(settings):
    rows = roles_for_job("Research Engineer, Code RL", profile=PROFILE, settings=settings)
    assert {r.group for r in rows} == set(GROUPS)
    assert len({r.title.casefold() for r in rows}) == len(rows)
    only = roles_for_job("x", profile=PROFILE, settings=settings, groups=("recruiter",))
    assert {r.group for r in only} == {"recruiter"}


def test_people_land_in_the_employer_database_grouped_by_what_found_them(
        settings, repos, stub_search):
    find_people("Anthropic", job_title="Research Engineer, Code RL (Reinforcement Learning)",
                settings=settings, profile=PROFILE, max_queries=40)
    found = {p["full_name"]: p for p in people_at("Anthropic", settings=settings)}
    assert found["Rita Recruits"]["group"] == "recruiter"
    assert found["Pete Peer"]["group"] == "peer"
    assert found["Ana Alumna"]["group"] == "alumni"
    assert found["Rita Recruits"]["profile_url"].endswith("/rita-recruits")
    assert list(found)[0] == "Rita Recruits", "recruiters first"

    # Nothing of it in PitWall's own database.
    main = repos.conn
    assert main.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert main.execute("SELECT 1 FROM companies WHERE name = 'Anthropic'").fetchone() is None
    assert employer_settings(settings).db_file != settings.db_file
    assert employer_settings(settings).db_file.exists()


def test_credits_stay_in_the_main_database(settings):
    repos = people_mod.employer_repos(settings)
    from pitwall.db import get_conn

    assert repos.quota.conn is get_conn(settings)
    assert repos.conn is not get_conn(settings)


def test_the_motorsport_list_is_not_touched(settings, stub_search, project):
    before = (project / "config" / "companies.csv").read_text(encoding="utf-8")
    find_people("Anthropic", job_title="Research Engineer", settings=settings, profile=PROFILE)
    assert (project / "config" / "companies.csv").read_text(encoding="utf-8") == before


def test_known_in_pitwall_matches_the_normalised_employer(repos):
    repos.candidates.upsert({"linkedin_slug": "a", "profile_url": "https://l/in/a",
                             "full_name": "A", "current_company": "Anthropic, Inc."})
    repos.candidates.upsert({"linkedin_slug": "b", "profile_url": "https://l/in/b",
                             "full_name": "B", "current_company": "Anthropology Ltd"})
    assert [p["full_name"] for p in known_in_pitwall(repos.conn, "Anthropic")] == ["A"]


def test_an_employer_never_searched_has_nobody(settings):
    assert people_at("Nobody GmbH", settings=settings) == []


@pytest.mark.parametrize("title, found_by, group", [
    ("Senior Talent Partner", {"hiring"}, "recruiter"),
    ("Engineering Manager, Perception", {"peer"}, "hiring"),
    ("SWE-II", {"hiring"}, "peer"),
    ("Research Engineer", {"hiring"}, "peer"),
    ("Engineering Manager", {"alumni", "hiring"}, "alumni"),
    ("", {"recruiter"}, "recruiter"),
    (None, set(), "hiring"),
])
def test_a_persons_own_title_decides_their_group(title, found_by, group):
    from pitwall.jobsearch.people import group_for

    assert group_for(title, found_by) == group


def test_the_owner_can_tell_the_email_chain_an_employers_domain(settings, stub_search):
    from pitwall.jobsearch.people import employer_domain, set_employer_domain

    assert set_employer_domain("Anthropic", "https://www.anthropic.com/careers",
                               settings=settings) == "anthropic.com"
    find_people("Anthropic", job_title="Research Engineer", settings=settings, profile=PROFILE)
    assert employer_domain("Anthropic", settings=settings) == "anthropic.com", \
        "carried into the employer list, so the search's sync keeps it"
    assert set_employer_domain("Anthropic", "anthropic.ai", settings=settings) == "anthropic.ai"
    assert employer_domain("Anthropic", settings=settings) == "anthropic.ai", "override wins"
    with pytest.raises(ValueError):
        set_employer_domain("Anthropic", "not a domain", settings=settings)
