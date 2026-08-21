"""SERP title -> person fields. The highest-ROI test file in the project (§12.1).

Every string below is a real shape seen in DuckDuckGo/Google results, including
the awkward ones: en-dash, `·`, non-Latin names, internal hyphens in titles,
country-mirror hosts, posts masquerading as profiles, and the concatenated
multi-result titles some metasearch backends emit.
"""

from __future__ import annotations

import pytest

from pitwall.extract.linkedin import (
    clean_serp_title,
    clean_snippet,
    is_profile_url,
    parse_profile,
)

# (title, snippet, url, expected_name, expected_title, expected_company)
CASES = [
    (
        "Jane Doe - Senior Data Scientist - McLaren Racing | LinkedIn",
        "Data Scientist at McLaren Racing · London, England, United Kingdom · 500+ connections",
        "https://uk.linkedin.com/in/jane-doe-1a2b3c",
        "Jane Doe", "Senior Data Scientist", "McLaren Racing",
    ),
    (
        "Luca Rossi – Race Strategy Engineer – Scuderia Ferrari HP | LinkedIn",
        "Maranello, Emilia-Romagna, Italy",
        "https://it.linkedin.com/in/luca-rossi-99",
        "Luca Rossi", "Race Strategy Engineer", "Scuderia Ferrari HP",
    ),
    (
        "Émilie Dubois · Simulation Engineer · Toyota Gazoo Racing | LinkedIn",
        "Cologne, North Rhine-Westphalia, Germany",
        "https://de.linkedin.com/in/emilie-dubois",
        "Émilie Dubois", "Simulation Engineer", "Toyota Gazoo Racing",
    ),
    (
        "Sam Patel - McLaren Racing | LinkedIn",
        "",
        "https://www.linkedin.com/in/sampatel",
        "Sam Patel", None, "McLaren Racing",
    ),
    (
        "Tom Green - Data Scientist - Vehicle Dynamics - Red Bull Racing | LinkedIn",
        "",
        "https://www.linkedin.com/in/tomgreen",
        "Tom Green", "Data Scientist - Vehicle Dynamics", "Red Bull Racing",
    ),
    (
        "Jean-Luc Picard - Head of Strategy - PREMA Racing | LinkedIn",
        "",
        "https://www.linkedin.com/in/jlpicard",
        "Jean-Luc Picard", "Head of Strategy", "PREMA Racing",
    ),
    (
        "张伟 - Data Engineer - Hitech GP | LinkedIn",
        "",
        "https://www.linkedin.com/in/zhang-wei-11",
        "张伟", "Data Engineer", "Hitech GP",
    ),
    (
        "Karl Mayer | LinkedIn",
        "Telemetry Engineer at Manthey · Meuspath, Rhineland-Palatinate, Germany",
        "https://de.linkedin.com/in/karl-mayer",
        "Karl Mayer", "Telemetry Engineer", "Manthey",
    ),
    (
        "Priya Nair - Data Scientist at Alpine Racing | LinkedIn",
        "",
        "https://www.linkedin.com/in/priya-nair",
        "Priya Nair", "Data Scientist", "Alpine Racing",
    ),
    (
        "Fabio Tosi - Managing Director at Af Corse Usa",
        "Returning to the pitwall this season",
        "https://www.linkedin.com/in/fabio-tosi-80196346/",
        "Fabio Tosi", "Managing Director", "Af Corse Usa",
    ),
    (
        "Jorge Segers - Race Engineer WEC at AF CORSE LTD | LinkedInUgo Maceratini - "
        "Free Lance Race Engineer | LinkedInVista Marston - Software Engineer | LinkedIn",
        "",
        "https://be.linkedin.com/in/jorgesegers",
        "Jorge Segers", "Race Engineer WEC", "AF CORSE LTD",
    ),
    (
        "Ana Silva on LinkedIn: #f1 #datascience great weekend at Zandvoort",
        "Ana Silva, Performance Engineer at Alpine, posted a photo",
        "https://www.linkedin.com/in/ana-silva-77",
        "Ana Silva", "Performance Engineer", "Alpine",
    ),
    (
        "David Brooks, PhD - Data Analyst - McLaren Health Care | LinkedIn",
        "Macomb, Michigan, United States",
        "https://www.linkedin.com/in/david-brooks-md",
        "David Brooks", "Data Analyst", "McLaren Health Care",
    ),
    (
        "Anna Petrova (she/her) 🏎️ - ML Engineer - Sauber Motorsport | LinkedIn",
        "",
        "https://ch.linkedin.com/in/anna-petrova",
        "Anna Petrova", "ML Engineer", "Sauber Motorsport",
    ),
    (
        "Marco Bianchi - Performance Engineer & Data Engineer - AF Corse | LinkedIn",
        "",
        "https://www.linkedin.com/in/marco-bianchi-af",
        "Marco Bianchi", "Performance Engineer & Data Engineer", "AF Corse",
    ),
]


@pytest.mark.parametrize(
    ("title", "snippet", "url", "name", "job", "employer"),
    CASES,
    ids=[c[3] for c in CASES],
)
def test_parses_real_serp_titles(title, snippet, url, name, job, employer):
    out = parse_profile(title, snippet, url)
    assert out is not None
    assert out["full_name"] == name
    assert out["current_title"] == job
    assert out["current_company"] == employer
    assert out["profile_url"].startswith("https://www.linkedin.com/in/")


NON_PROFILES = [
    "https://www.linkedin.com/posts/jane-doe_activity-7123",
    "https://www.linkedin.com/pulse/why-f1-loves-data-jane-doe",
    "https://www.linkedin.com/company/mclaren-racing",
    "https://www.linkedin.com/jobs/view/data-scientist-at-mclaren-123",
    "https://www.linkedin.com/school/imperial-college-london",
    "https://www.linkedin.com/showcase/mclaren-applied",
    "https://www.example.com/in/jane-doe",
]


@pytest.mark.parametrize("url", NON_PROFILES)
def test_rejects_non_profile_urls(url):
    assert parse_profile("Anything | LinkedIn", "", url) is None
    assert is_profile_url(url) is False


def test_country_mirrors_share_one_slug():
    a = parse_profile("Jane Doe | LinkedIn", "", "https://uk.linkedin.com/in/jane-doe")
    b = parse_profile("Jane Doe | LinkedIn", "", "https://www.linkedin.com/in/Jane-Doe/")
    assert a["linkedin_slug"] == b["linkedin_slug"] == "jane-doe"


def test_location_is_extracted_from_the_snippet():
    out = parse_profile(
        "Jane Doe - Data Scientist - McLaren Racing | LinkedIn",
        "Woking, England, United Kingdom · 500+ connections",
        "https://www.linkedin.com/in/jane-doe",
    )
    assert out["location"] == "Woking, England, United Kingdom"


def test_falls_back_to_the_slug_when_the_title_is_useless():
    out = parse_profile("", "", "https://www.linkedin.com/in/marta-silva-4b2a1c9")
    assert out["full_name"] == "Marta Silva"


def test_concatenated_titles_are_cut_to_the_first_result():
    glued = (
        "Oliviero Ferrari - Data Engineer | LinkedInEmiliano Ferrari - "
        "Data Engineer at Goldman Sachs | LinkedIn"
    )
    assert clean_serp_title(glued) == "Oliviero Ferrari - Data Engineer"


def test_ellipsis_glued_titles_are_cut():
    glued = (
        "Tabassum F. - Sports Partnerships @ Scuderia Ferrari ...Jorge Segers - "
        "Race Engineer WEC at AF CORSE LTD"
    )
    assert clean_serp_title(glued) == "Tabassum F. - Sports Partnerships @ Scuderia Ferrari"


def test_a_genuine_trailing_ellipsis_survives():
    assert clean_serp_title("Jane Doe - Data Scientist working on ...") == (
        "Jane Doe - Data Scientist working on"
    )


def test_absurdly_long_names_are_rejected():
    out = parse_profile("x" * 120 + " - Engineer - Team", "", "https://www.linkedin.com/in/foo-bar")
    assert out["full_name"] == "Foo Bar"  # recovered from the slug instead


def test_connection_noise_is_stripped_from_snippets():
    assert "connections" not in clean_snippet("Data Scientist · 500+ connections · London")
    assert "London" in clean_snippet("Data Scientist · 500+ connections · London")
