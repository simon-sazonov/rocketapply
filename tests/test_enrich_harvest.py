"""Address harvesting — §2.2, the highest-value step.

Two things are being protected here. The first is the corpus: a role address,
a false name or another company's address in `domain_address_samples` misleads
the pattern inference for every future candidate at that company, so the
negative tests matter more than the positive one. The second is the line §0.3
draws — this step reads what the search index returned, and follows a link only
to finish an address a snippet cut in half. It never crawls.
"""

from __future__ import annotations

import pytest

from pitwall.enrich.harvest import (
    HARVEST_QUERIES,
    MAX_PAGE_FETCHES,
    HarvestStep,
    attribute,
    harvest_domain,
    known_people_at,
    name_from_title,
    name_near,
)
from tests.conftest import StubSearcher, serp


def _pages(*hits):
    """Every harvest query answers with the same page set."""
    return {"@alpine.com": list(hits)}


# ─────────────────────────── name attribution ────────────────────────────


def test_a_name_beside_the_address_is_read():
    assert name_near(
        "Contact Jane Doe on jane.doe@alpine.com for media enquiries",
        "jane.doe@alpine.com",
    ) == "Jane Doe"


def test_a_leading_noise_word_does_not_hide_the_name():
    """A single whole-span regex matches "Contact Jane Doe", gets rejected for
    containing "Contact", and then resumes past the name it was looking for."""
    assert name_near("Contact Jane Doe: jane@alpine.com", "jane@alpine.com") == "Jane Doe"
    assert name_near("Email Marc Dubois — marc@alpine.com", "marc@alpine.com") == "Marc Dubois"


def test_a_role_word_before_the_name_does_not_become_the_first_name():
    assert name_near(
        "Head of Aero Marc Dubois - marc.dubois@alpine.com", "marc.dubois@alpine.com"
    ) == "Marc Dubois"


def test_the_nearest_name_wins_when_a_page_lists_several_people():
    """A contact page runs "Marc Dubois marc.dubois@… and Anna Klein anna.klein@…".
    Scanning the whole window left to right attributes every address on the
    page to whoever is named first on it."""
    text = "Marc Dubois marc.dubois@alpine.com and Anna Klein anna.klein@alpine.com"
    assert name_near(text, "marc.dubois@alpine.com") == "Marc Dubois"
    assert name_near(text, "anna.klein@alpine.com") == "Anna Klein"


def test_no_name_in_the_prose_returns_none():
    assert name_near("please email press@alpine.com or click here", "press@alpine.com") is None
    assert name_near("jane.doe@alpine.com", "jane.doe@alpine.com") is None


def test_a_shouted_organisation_name_is_not_read_as_a_person():
    """A wrong name is worse than no name: it enters the corpus as a false
    sample and misleads the inference for everyone else at that company."""
    assert name_near("PREMA RACING contact@prema.org.uk", "contact@prema.org.uk") is None
    assert name_from_title("AF CORSE | Official Site") is None


def test_a_title_leads_with_the_persons_name():
    assert name_from_title("Jane Doe - Head of Aerodynamics | Alpine") == "Jane Doe"
    assert name_from_title("Contact us | Alpine") is None


def test_attribute_prefers_the_snippet_then_falls_back_to_the_title():
    hit = serp("https://alpine.com/team", "Jane Doe - Aero | Alpine", "email jane.doe@alpine.com")
    assert attribute(hit, "jane.doe@alpine.com") == "Jane Doe"


def test_the_address_is_never_used_to_invent_the_name():
    """Deriving the name FROM the address and then matching it back would make
    every inference circular and every pattern trivially "confirmed"."""
    hit = serp("https://alpine.com/contact", "Contact | Alpine", "jane.doe@alpine.com")
    assert attribute(hit, "jane.doe@alpine.com") is None


# ────────────────────────────── the harvest ──────────────────────────────


def test_addresses_are_pulled_out_of_the_serps(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://alpine.com/team", "Team | Alpine",
             "Marc Dubois marc.dubois@alpine.com and Anna Klein anna.klein@alpine.com"),
    )))
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    found = {h.email: h.person_name for h in report.found}
    assert found == {
        "marc.dubois@alpine.com": "Marc Dubois",
        "anna.klein@alpine.com": "Anna Klein",
    }
    assert report.queries_run == len(HARVEST_QUERIES)


def test_the_three_queries_of_the_brief_are_the_ones_that_run(enrich_ctx):
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    harvest_domain(ctx, "alpine.com", "Alpine")
    assert searcher.queries == [
        'site:alpine.com "@alpine.com"',
        '"@alpine.com" -site:linkedin.com',
        '"Alpine" "@alpine.com" contact',
    ]


def test_only_addresses_at_this_domain_are_harvested(enrich_ctx):
    """Another company's address in this corpus corrupts the inference."""
    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://x/", "T", "marc.dubois@alpine.com and an advert bob@dealer.co.uk"),
    )))
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert [h.email for h in report.found] == ["marc.dubois@alpine.com"]


def test_role_addresses_are_skipped_and_counted(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://x/", "T", "info@alpine.com press@alpine.com marc.dubois@alpine.com"),
    )))
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert [h.email for h in report.found] == ["marc.dubois@alpine.com"]
    assert report.role_addresses_skipped >= 2


def test_a_name_found_on_a_later_page_fills_in_an_earlier_gap(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher({
        'site:alpine.com': [serp("https://a/", "Contact", "marc.dubois@alpine.com")],
        "-site:linkedin.com": [
            serp("https://b/", "News", "Marc Dubois (marc.dubois@alpine.com) said")
        ],
    }))
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert [(h.email, h.person_name) for h in report.found] == [
        ("marc.dubois@alpine.com", "Marc Dubois")
    ]


def test_a_failing_query_does_not_end_the_harvest(enrich_ctx):
    searcher = StubSearcher(_pages(serp("https://x/", "T", "Marc Dubois marc@alpine.com")))
    searcher.fail_on = {"site:alpine.com"}
    ctx = enrich_ctx(searcher=searcher)
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert [h.email for h in report.found] == ["marc@alpine.com"]
    assert any("harvest query failed" in n for n in report.notes)


def test_no_searcher_is_a_note_not_a_crash(enrich_ctx):
    ctx = enrich_ctx(searcher=None)
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert report.found == []
    assert any("no search provider" in n for n in report.notes)


def test_a_provider_without_operators_refuses_rather_than_returning_noise(enrich_ctx):
    """`site:` and quoted phrases ARE the technique. A provider that drops them
    does not fail, it answers something else — the v1 failure mode."""

    class NoOperators(StubSearcher):
        def supports_operators(self):
            return False

    ctx = enrich_ctx(searcher=NoOperators(_pages(serp("https://x/", "T", "a@alpine.com"))))
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert report.found == []
    assert any("does not support operators" in n for n in report.notes)


def test_the_corpus_is_capped(enrich_ctx):
    many = " ".join(f"person{i}.surname{i}@alpine.com" for i in range(60))
    ctx = enrich_ctx(searcher=StubSearcher(_pages(serp("https://x/", "T", many))))
    report = harvest_domain(ctx, "alpine.com", "Alpine")
    assert len(report.found) <= ctx.settings.enrich_max_samples


# ───────────────── finishing an address a snippet cut in half ────────────


def test_a_truncated_snippet_is_confirmed_by_reading_that_one_page(enrich_ctx):
    """§2.2: "fetching that one indexed page to confirm is in scope"."""
    fetched = []

    def fetcher(url):
        fetched.append(url)
        return "Marc Dubois marc.dubois@alpine.com and Anna Klein anna.klein@alpine.com"

    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://alpine.com/contact", "Contact", "write to marc.dubois@alp…"),
    )))
    report = harvest_domain(ctx, "alpine.com", "Alpine", fetcher=fetcher)
    assert fetched == ["https://alpine.com/contact"]
    assert {h.email for h in report.found} >= {
        "marc.dubois@alpine.com", "anna.klein@alpine.com"
    }


def test_nothing_is_fetched_when_the_corpus_is_already_good_enough(enrich_ctx):
    """A confirmation step, not a discovery one — it stops as soon as the
    corpus can settle a format."""
    def fetcher(url):
        raise AssertionError("should not have fetched anything")

    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://x/", "T",
             "Marc Dubois marc.dubois@alpine.com Anna Klein anna.klein@alpine.com …"),
    )))
    report = harvest_domain(ctx, "alpine.com", "Alpine", fetcher=fetcher)
    assert report.pages_fetched == 0


def test_page_fetches_are_capped(enrich_ctx):
    calls = []

    def fetcher(url):
        calls.append(url)
        return "nothing useful here"

    hits = [serp(f"https://alpine.com/p{i}", "T", "email marc@alp…") for i in range(10)]
    ctx = enrich_ctx(searcher=StubSearcher(_pages(*hits)))
    harvest_domain(ctx, "alpine.com", "Alpine", fetcher=fetcher)
    assert len(calls) <= MAX_PAGE_FETCHES


def test_a_refused_fetch_is_recorded_and_survivable(enrich_ctx):
    def fetcher(url):
        raise RuntimeError("robots.txt disallows this")

    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://alpine.com/contact", "Contact", "write to marc.dubois@alp…"),
    )))
    report = harvest_domain(ctx, "alpine.com", "Alpine", fetcher=fetcher)
    assert any("could not read" in n for n in report.notes)


# ─────────────────────────────── the step ────────────────────────────────


def test_the_step_stores_the_corpus_against_the_company(enrich_ctx, repos):
    ctx = enrich_ctx(searcher=StubSearcher(_pages(
        serp("https://alpine.com/team", "Team",
             "Marc Dubois marc.dubois@alpine.com Anna Klein anna.klein@alpine.com"),
    )))
    ctx.domain = "alpine.com"
    ctx.company_domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")

    outcome = HarvestStep().run(ctx)
    assert outcome.ok
    assert outcome.samples_added == 2
    stored = repos.domains.samples(ctx.company_domain_id)
    assert {s["email"] for s in stored} == {
        "marc.dubois@alpine.com", "anna.klein@alpine.com"
    }
    assert repos.domains.by_id(ctx.company_domain_id)["last_harvested_at"]


def test_a_recent_harvest_is_reused_instead_of_re_searching(enrich_ctx, repos):
    """The whole point of caching per company: one harvest of alpine.com
    resolves the format for every Alpine candidate you will ever look at."""
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    ctx.domain = "alpine.com"
    domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")
    ctx.company_domain_id = domain_id
    repos.domains.add_sample(domain_id, "marc.dubois@alpine.com", person_name="Marc Dubois")
    repos.domains.add_sample(domain_id, "anna.klein@alpine.com", person_name="Anna Klein")
    repos.domains.mark_harvested(domain_id)

    outcome = HarvestStep().run(ctx)
    assert outcome.ok
    assert searcher.queries == []
    assert "harvested recently" in (outcome.detail or "")
    assert len(ctx.samples) == 2


def test_a_thin_cached_corpus_is_re_harvested(enrich_ctx, repos):
    """One sample cannot settle a format, so the cache does not apply."""
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    ctx.domain = "alpine.com"
    domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")
    ctx.company_domain_id = domain_id
    repos.domains.add_sample(domain_id, "marc.dubois@alpine.com", person_name="Marc Dubois")
    repos.domains.mark_harvested(domain_id)

    HarvestStep().run(ctx)
    assert searcher.queries != []


def test_force_re_harvests_regardless(enrich_ctx, repos):
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    ctx.domain = "alpine.com"
    domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")
    ctx.company_domain_id = domain_id
    for email, name in [("a.b@alpine.com", "Aa Bb"), ("c.d@alpine.com", "Cc Dd")]:
        repos.domains.add_sample(domain_id, email, person_name=name)
    repos.domains.mark_harvested(domain_id)

    HarvestStep(force=True).run(ctx)
    assert searcher.queries != []


def test_the_step_skips_without_a_domain(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher())
    assert HarvestStep().run(ctx).outcome == "skipped"


def test_the_step_is_free():
    assert HarvestStep.spends_credits is False


def test_known_people_are_everyone_at_the_same_employer(enrich_ctx, repos):
    ctx = enrich_ctx()
    repos.candidates.upsert({
        "linkedin_slug": "marc-dubois", "profile_url": "https://x/in/marc-dubois",
        "full_name": "Marc Dubois", "current_company": "Alpine",
    })
    repos.candidates.upsert({
        "linkedin_slug": "elsewhere", "profile_url": "https://x/in/elsewhere",
        "full_name": "Someone Else", "current_company": "Ferrari",
    })
    names = {p["full_name"] for p in known_people_at(ctx)}
    assert "Marc Dubois" in names
    assert "Someone Else" not in names


@pytest.mark.parametrize("template", HARVEST_QUERIES)
def test_every_harvest_query_uses_an_operator(template):
    """Without operators these queries return the open web, not the company."""
    assert any(op in template for op in ('site:', '"'))
