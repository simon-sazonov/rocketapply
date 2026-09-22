"""The job search's extraction rules — pure functions, no database.

Every case here is either one of the spec's §89 test jobs or a trap found in
the live Arbeitnow feed on 22 Sep 2026 while building this: "AI Deployment
Strategist", "Member of Technical Staff", "Cambridge, MA", "(m/w/d)", German
experience phrasing, and a sponsorship refusal split across two sentences.

The rule most of them defend is §90's: a false rejection is the expensive
mistake, so an unknown must stay unknown.
"""

from __future__ import annotations

import pytest

from pitwall.jobsearch import experience, roles, visa
from pitwall.jobsearch.companies import normalize_company, normalize_title, title_key
from pitwall.jobsearch.locations import country_code, parse_location, region_contains
from pitwall.jobsearch.normalize import (
    canonical_url,
    education,
    employment_types,
    languages_required,
    parse_salary,
)
from pitwall.jobsearch.skills import parse_vocabulary

# ───────────────────────────── locations (§58-59) ────────────────────────


@pytest.mark.parametrize(("text", "countries", "cities"), [
    ("London", ("GB",), ("London",)),
    ("München", ("DE",), ("Munich",)),
    ("Cambridge, MA", ("US",), ("Cambridge",)),
    ("Cambridge, UK", ("GB",), ("Cambridge",)),
    ("San Francisco, CA", ("US",), ("San Francisco",)),
    ("Paris office", ("FR",), ("Paris",)),
    ("Berlin, Berlin, Deutschland", ("DE",), ("Berlin",)),
    ("Dusseldorf; Frankfurt; Munich", ("DE",), ("Dusseldorf", "Frankfurt", "Munich")),
    ("Lisboa, Portugal (Hybrid)", ("PT",), ("Lisbon",)),
    ("Somewhere nobody has heard of", (), ()),
])
def test_a_city_is_placed_in_its_country_without_the_posting_saying_so(text, countries, cities):
    loc = parse_location(text)
    assert loc.countries == countries
    assert loc.cities == cities


def test_remote_us_only_is_a_remote_job_restricted_to_the_us():
    loc = parse_location("Remote – US only")
    assert (loc.remote_status, loc.remote_region, loc.countries) == ("remote", "US", ("US",))


def test_remote_eu_is_a_region_not_a_country():
    loc = parse_location("Remote (EU)")
    assert loc.remote_region == "EU" and loc.countries == ()
    assert region_contains("EU", "PT") and not region_contains("EU", "GB")
    assert region_contains("EUROPE", "GB")


def test_the_source_s_own_workplace_field_outranks_the_text():
    assert parse_location("London", workplace="hybrid").remote_status == "hybrid"
    assert parse_location("Berlin", remote_flag=False).remote_status == "onsite"


def test_distributed_systems_is_not_a_remote_job():
    assert parse_location("Berlin", title="Distributed Systems Engineer").remote_status != "remote"


def test_country_names_codes_and_native_spellings_agree():
    assert country_code("Deutschland") == country_code("DE") == country_code("germany") == "DE"
    assert country_code("UK") == country_code("United Kingdom") == "GB"
    assert country_code("Atlantis") is None


# ─────────────────────────────── experience (§15) ────────────────────────


def test_three_plus_years_in_the_body_beats_junior_in_the_title():
    exp = experience.extract("Junior Data Scientist",
                             "You have 3+ years of professional experience in ML.")
    assert exp.min_years == 3 and exp.required


def test_a_range_is_read_as_min_and_max():
    exp = experience.extract("Data Scientist", "0-2 years of experience in data science.")
    assert (exp.min_years, exp.max_years, exp.level) == (0, 2, "entry")


def test_no_number_means_unknown_not_zero():
    exp = experience.extract("Data Scientist", "Experience with ML projects preferred.")
    assert exp.min_years is None and not exp.known


def test_company_history_is_not_an_experience_requirement():
    exp = experience.extract("Data Scientist", "Founded 20 years ago, we lead our market.")
    assert exp.min_years is None


def test_preferred_years_are_kept_but_not_required():
    exp = experience.extract(
        "Data Scientist",
        "At least two years of experience with SQL. 5+ years of experience in a similar "
        "role is a plus.",
    )
    assert exp.min_years == 2 and exp.required


def test_the_binding_requirement_is_the_largest_required_minimum():
    exp = experience.extract("ML Engineer",
                             "2+ years of Python experience and 5+ years of industry experience.")
    assert exp.min_years == 5


@pytest.mark.parametrize(("text", "years"), [
    ("Mindestens 3 Jahre Berufserfahrung im Bereich Data Science.", 3),
    ("Vous avez au moins 2 ans d'expérience.", 2),
])
def test_german_and_french_phrasings_are_read(text, years):
    assert experience.extract("Data Scientist", text).min_years == years


def test_a_graduate_title_with_a_silent_body_is_entry_level():
    exp = experience.extract("Graduate ML Engineer", "Python and PyTorch.")
    assert exp.level == "entry" and exp.min_years is None


@pytest.mark.parametrize("text", [
    "Strong candidates will have a track record of solving large-scale systems problems.",
    "You bring deep expertise in distributed training.",
    "You will mentor junior engineers on the team.",
    "Mehrjährige Berufserfahrung in Python.",
])
def test_seniority_without_a_number_scores_down_but_never_filters(text):
    exp = experience.extract("Research Engineer", text)
    assert exp.level == "mid" and exp.min_years is None and not exp.required


def test_an_entry_title_outranks_body_seniority_language():
    exp = experience.extract("Graduate Research Engineer", "A track record of shipping.")
    assert exp.level == "entry"


def test_member_of_technical_staff_is_not_a_staff_level_title():
    assert experience.title_level("Member of Technical Staff (Applied AI)") == "unknown"
    assert experience.title_level("Staff ML Engineer") == "senior"


# ────────────────────────────────── visa (§22) ───────────────────────────


def test_silence_is_unknown_never_no():
    assert visa.extract("Python, SQL, a friendly team.").status == visa.UNKNOWN


def test_an_explicit_offer_is_explicit_and_quoted():
    v = visa.extract("Great team. We sponsor work visas for international hires.")
    assert v.status == visa.SPONSORSHIP_EXPLICIT
    assert "sponsor work visas" in v.evidence


def test_we_are_unable_to_sponsor_is_a_refusal_even_though_it_says_sponsor():
    assert visa.extract("We are unable to sponsor visas.").status == visa.NO_SPONSORSHIP


def test_a_refusal_split_across_sentences_keeps_its_country():
    v = visa.extract("You must be authorized to work in the U.S. We are unable to sponsor.")
    assert v.status == visa.NO_SPONSORSHIP and v.scope == "US"


def test_join_us_is_not_the_united_states():
    v = visa.extract("Applicants must have the right to work in Germany to join us.")
    assert v.scope == "DE"


def test_contradictory_text_is_unknown_with_both_sentences():
    v = visa.extract("We sponsor visas where needed. Sponsorship is not available for contractors.")
    assert v.status == visa.UNKNOWN and v.evidence.startswith("mixed signals")


def test_relocation_is_recorded_separately_from_the_visa():
    v = visa.extract("A relocation package is provided.")
    assert v.relocation and v.status == visa.UNKNOWN


def test_the_owners_company_note_lifts_only_unknown():
    assert visa.extract("Nothing here.", company_sponsorship="yes").status == visa.LIKELY_SUPPORTED
    refused = visa.extract("We cannot sponsor visas.", company_sponsorship="yes")
    assert refused.status == visa.NO_SPONSORSHIP


# ──────────────────────────────── roles (§13) ────────────────────────────


@pytest.mark.parametrize(("title", "expected"), [
    ("Decision Intelligence Scientist", {"DECISION_SCIENCE"}),
    ("ML Engineer", {"MACHINE_LEARNING", "AI", "SOFTWARE_ENGINEERING"}),
    ("Graduate Machine Learning Engineer", {"MACHINE_LEARNING", "AI", "SOFTWARE_ENGINEERING"}),
    ("Operations Research Scientist", {"OPTIMIZATION", "APPLIED_SCIENCE"}),
    ("Machine Learning Intern", {"MACHINE_LEARNING", "AI"}),
    ("Werkstudent Data Science (m/w/d)", {"DATA_SCIENCE"}),
    ("Product Data Analyst", {"ANALYTICS"}),
])
def test_roles_are_classified_by_meaning_not_exact_title(title, expected):
    assert set(roles.classify(title).labels) == expected


@pytest.mark.parametrize("title", [
    "AI Deployment Strategist",
    "Affiliate Manager (AI Product)",
    "Investment Banking Intern (AI Evaluation)",
    "Mitarbeiter Datenmanagement & KI (m/w/d)",
])
def test_a_business_role_that_mentions_ai_is_not_an_ai_role(title):
    assert roles.classify(title).labels == ("OTHER",)


def test_a_generic_title_is_decided_by_repeated_evidence_in_the_body():
    result = roles.classify(
        "Engineer", "Build our AI platform with LLMs. Generative models, LLM evals, AI agents."
    )
    assert "AI" in result.labels and result.from_description and result.confidence < 0.9


def test_a_research_role_outside_ml_and_data_is_not_ours():
    result = roles.classify("Research Scientist, Quantum Chemistry",
                            "We study reaction kinetics in the lab.")
    assert result.labels == ("OTHER",)


# ─────────────────────── names, titles, URLs (§5.2, §11) ─────────────────


def test_company_legal_forms_do_not_split_one_employer():
    assert normalize_company("BMW") == normalize_company("BMW Group") == \
        normalize_company("BMW AG") == "bmw"
    assert normalize_company("Tulip Logistics B.V.") == "tulip logistics"


def test_title_key_ignores_word_order_and_gender_markers():
    assert title_key("Graduate Machine Learning Engineer") == \
        title_key("Machine Learning Engineer – Graduate")
    assert title_key("Data Scientist (m/w/d)") == title_key("Data Scientist")
    assert normalize_title("Sr. ML Engineer") == "senior machine learning engineer"


def test_tracking_parameters_do_not_make_a_different_url():
    assert canonical_url("https://www.boards.greenhouse.io/x/jobs/1/?gh_src=a&utm_source=li") \
        == canonical_url("https://boards.greenhouse.io/x/jobs/1")


# ─────────────────────── skills, salary, education, language ─────────────


VOCAB = {
    "python": {"label": "Python", "synonyms": ["python"]},
    "r_lang": {"label": "R", "case_sensitive": True, "synonyms": ["R"]},
    "scikit_learn": {"label": "scikit-learn", "synonyms": ["scikit-learn", "sklearn"]},
    "machine_learning": {"label": "ML", "synonyms": ["machine learning"]},
    "ml_abbrev": {"case_sensitive": True, "synonyms": ["ML"], "alias_of": "machine_learning"},
    "cpp": {"label": "C++", "synonyms": ["c++"]},
    "excel": {"label": "Excel", "case_sensitive": True, "synonyms": ["Excel"]},
}


def test_synonyms_map_to_one_canonical_skill():
    vocab = parse_vocabulary(VOCAB)
    found = vocab.extract("sklearn, machine-learning and ML in production; C++ and Python or R.")
    assert set(found) == {"scikit_learn", "machine_learning", "cpp", "python", "r_lang"}


def test_ordinary_words_are_not_skills():
    vocab = parse_vocabulary(VOCAB)
    assert vocab.extract("You excel at R&D.") == []


def test_one_spelling_cannot_mean_two_skills():
    with pytest.raises(ValueError, match="both"):
        parse_vocabulary({"a": {"synonyms": ["spark"]}, "b": {"synonyms": ["spark"]}})


@pytest.mark.parametrize(("text", "lo", "hi", "cur"), [
    ("Salary: €60k–€75k per year", 60000, 75000, "EUR"),
    ("60.000 - 75.000 EUR", 60000, 75000, "EUR"),
    ("€5,000 - 6,000/month", 60000, 72000, "EUR"),
    ("$120k-150k", 120000, 150000, "USD"),
])
def test_salaries_are_normalised_to_annual(text, lo, hi, cur):
    s = parse_salary(text)
    assert (s["salary_min"], s["salary_max"], s["salary_currency"]) == (lo, hi, cur)


def test_a_range_without_a_currency_is_not_a_salary():
    assert parse_salary("We grew 60-75% last year") == {}


def test_phd_required_only_when_nothing_softens_it():
    assert education("A PhD in Computer Science is required.") == ("phd", None)
    assert education("MSc or PhD in a quantitative field required.")[0] == "masters"
    assert education("PhD or equivalent experience.") == (None, "phd")
    assert education("Opportunities for promotion; you must be curious.") == (None, None)


def test_employment_types_come_from_the_source_and_the_title():
    assert employment_types("Full-time permanent", "Working Student (m/f/d)") == \
        ["full_time", "permanent", "working_student"]
    assert employment_types(None, "Graduate Data Scientist") == []


def test_a_required_language_is_noticed():
    assert languages_required("Data Scientist", "Fluent German is required.", "en") == ["de"]
    assert languages_required("Data Scientist", "English only.", "en") == []


def test_greenhouse_entity_encoded_html_comes_out_as_text():
    from pitwall.sources.jobs.base import strip_html

    text = strip_html("&lt;p&gt;Join us.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; SQL"
                      "&lt;/li&gt;&lt;li&gt;3+ years&lt;/li&gt;&lt;/ul&gt;")
    assert "<" not in text and "Python & SQL" in text and "3+ years" in text
