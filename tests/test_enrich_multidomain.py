"""A works team is several organisations sharing a name.

Alpine runs an F1 entry on one domain and a sportscar programme on another.
Toyota Gazoo Racing, BMW M Motorsport, Ferrari and Peugeot are the same shape.
A LinkedIn headline that says "Alpine" does not say which one, and no amount of
string matching will tell you — so the chain works through every domain the
company is known to use and produces one candidate address per organisation,
each carrying its own evidence.

Picking one and hiding the rest would be a guess wearing a single answer's
clothes, which is the failure §4 exists to prevent.
"""

from __future__ import annotations

import pytest

from pitwall import enrich as E
from pitwall.enrich.domains import MAX_DOMAINS, DomainStep
from pitwall.enrich.harvest import HarvestStep
from pitwall.enrich.patterns import PatternStep
from tests.conftest import StubSearcher, serp

F1_PAGE = serp(
    "https://alpinef1.com/team",
    "The team | Alpine F1",
    "Marc Dubois marc.dubois@alpinef1.com · Anna Klein anna.klein@alpinef1.com",
)
CARS_PAGE = serp(
    "https://alpinecars.com/endurance",
    "Endurance | Alpine",
    "Luc Bernard l.bernard@alpinecars.com · Sofia Rossi s.rossi@alpinecars.com",
)


@pytest.fixture()
def two_domains(enrich_ctx, repos):
    """Alpine, with both of its organisations already recorded."""
    ctx = enrich_ctx(searcher=StubSearcher({
        "@alpinef1.com": [F1_PAGE],
        "@alpinecars.com": [CARS_PAGE],
    }))
    company_id = int(ctx.company["id"])
    repos.domains.upsert(company_id, "alpinef1.com", source="config")
    repos.domains.upsert(company_id, "alpinecars.com", source="config")
    return ctx, company_id


# ───────────────────────────── the domain step ───────────────────────────


def test_every_known_domain_is_loaded_not_just_the_best(two_domains):
    ctx, _ = two_domains
    outcome = DomainStep().run(ctx)

    assert outcome.ok
    assert ctx.domains == ["alpinef1.com", "alpinecars.com"]
    assert ctx.domain == "alpinef1.com"          # the primary is still first
    assert "also alpinecars.com" in (outcome.detail or "")


def test_the_owners_override_leads_the_list(two_domains, repos):
    """A correction outranks every automatic source, and still does not delete
    the others — the person may work for either organisation."""
    ctx, company_id = two_domains
    repos.domains.set_override(company_id, "alpinecars.com")
    DomainStep().run(ctx)
    assert ctx.domains[0] == "alpinecars.com"
    assert "alpinef1.com" in ctx.domains


def test_the_number_of_domains_is_capped(enrich_ctx, repos):
    """A works team is two or three organisations; beyond that the extra
    addresses are noise rather than coverage."""
    ctx = enrich_ctx(searcher=StubSearcher())
    company_id = int(ctx.company["id"])
    for i in range(6):
        repos.domains.upsert(company_id, f"alpine{i}.com", source="config")
    DomainStep().run(ctx)
    assert len(ctx.domains) == MAX_DOMAINS


def test_a_single_domain_company_is_unchanged(enrich_ctx, repos):
    ctx = enrich_ctx(searcher=StubSearcher())
    repos.domains.upsert(int(ctx.company["id"]), "alpine.com", source="config")
    DomainStep().run(ctx)
    assert ctx.domains == ["alpine.com"]
    assert ctx.domain == "alpine.com"
    assert ctx.company_domain_id is not None


# ──────────────────────────── harvest, per domain ────────────────────────


def test_each_domain_gets_its_own_corpus(two_domains, repos):
    ctx, _ = two_domains
    DomainStep().run(ctx)
    outcome = HarvestStep().run(ctx)

    assert outcome.ok
    f1 = {s["email"] for s in ctx.samples_for("alpinef1.com")}
    cars = {s["email"] for s in ctx.samples_for("alpinecars.com")}
    assert f1 == {"marc.dubois@alpinef1.com", "anna.klein@alpinef1.com"}
    assert cars == {"l.bernard@alpinecars.com", "s.rossi@alpinecars.com"}
    # And they do not contaminate each other, which would wreck both patterns.
    assert not (f1 & cars)


def test_the_detail_names_every_domain_it_worked_on(two_domains):
    ctx, _ = two_domains
    DomainStep().run(ctx)
    detail = HarvestStep().run(ctx).detail or ""
    assert "alpinef1.com" in detail
    assert "alpinecars.com" in detail


# ─────────────────────── one candidate per organisation ──────────────────


def test_a_candidate_address_is_produced_for_each_domain(two_domains, mx_ok):
    ctx, _ = two_domains
    DomainStep().run(ctx)
    HarvestStep().run(ctx)
    outcome = PatternStep(resolver=mx_ok).run(ctx)

    addresses = {c.email for c in outcome.found}
    assert addresses == {"jane.doe@alpinef1.com", "j.doe@alpinecars.com"}


def test_each_address_is_scored_on_its_own_domains_evidence(two_domains, mx_ok):
    """The two organisations use different formats, and both are inferred
    correctly rather than one being imposed on the other."""
    ctx, _ = two_domains
    DomainStep().run(ctx)
    HarvestStep().run(ctx)
    found = {c.email: c for c in PatternStep(resolver=mx_ok).run(ctx).found}

    f1 = found["jane.doe@alpinef1.com"]
    cars = found["j.doe@alpinecars.com"]
    assert f1.evidence["pattern"] == "first.last"
    assert cars.evidence["pattern"] == "f.last"
    assert f1.status == cars.status == "probable"
    assert f1.evidence["sample_size"] == cars.evidence["sample_size"] == 2


def test_each_address_records_which_organisation_it_belongs_to(two_domains, mx_ok):
    """The reader has to be able to tell the two apart."""
    ctx, _ = two_domains
    DomainStep().run(ctx)
    HarvestStep().run(ctx)
    for candidate in PatternStep(resolver=mx_ok).run(ctx).found:
        assert candidate.evidence["derived_from_domain"] in ctx.domains
        assert candidate.evidence["company_domains"] == ctx.domains


def test_the_owner_is_told_the_company_has_more_than_one(two_domains, mx_ok):
    ctx, _ = two_domains
    DomainStep().run(ctx)
    HarvestStep().run(ctx)
    PatternStep(resolver=mx_ok).run(ctx)
    assert any("uses 2 domains" in n for n in ctx.notes)
    assert any("which one this person actually works for" in n for n in ctx.notes)


def test_a_single_domain_company_gets_no_such_note(enrich_ctx, repos, mx_ok):
    ctx = enrich_ctx(searcher=StubSearcher({"@alpine.com": [serp(
        "https://alpine.com/team", "Team",
        "Marc Dubois marc.dubois@alpine.com Anna Klein anna.klein@alpine.com",
    )]}))
    repos.domains.upsert(int(ctx.company["id"]), "alpine.com", source="config")
    DomainStep().run(ctx)
    HarvestStep().run(ctx)
    found = PatternStep(resolver=mx_ok).run(ctx).found
    assert len(found) == 1
    assert "company_domains" not in found[0].evidence
    assert not any("uses 2 domains" in n for n in ctx.notes)


# ────────────────────────────── end to end ───────────────────────────────


def test_both_addresses_are_stored_and_ranked(two_domains, repos, settings, mx_ok):
    ctx, _ = two_domains
    steps = [DomainStep(), HarvestStep(), PatternStep(resolver=mx_ok)]
    result = E.enrich_candidate(
        ctx.candidate_id, repos, settings, searcher=ctx.searcher, steps=steps
    )

    stored = {row["email"]: row for row in repos.contacts.for_candidate(ctx.candidate_id)}
    assert set(stored) == {"jane.doe@alpinef1.com", "j.doe@alpinecars.com"}
    assert all(row["status"] == "probable" for row in stored.values())
    assert len(result.ranked()) == 2


def test_corroboration_can_promote_one_organisation_over_the_other(
    two_domains, repos, settings, mx_ok
):
    """Which organisation the person is actually in is exactly what a published
    address settles, and the free verifier can settle it."""
    from pitwall.enrich.corroborate import CorroborateStep

    ctx, _ = two_domains
    ctx.searcher.pages['"j.doe@alpinecars.com"'] = [
        serp("https://alpinecars.com/contact", "Contact",
             "Jane Doe, Endurance — j.doe@alpinecars.com")
    ]
    steps = [DomainStep(), HarvestStep(), PatternStep(resolver=mx_ok), CorroborateStep()]
    result = E.enrich_candidate(
        ctx.candidate_id, repos, settings, searcher=ctx.searcher, steps=steps
    )

    best = result.best()
    assert best.email == "j.doe@alpinecars.com"
    assert best.status == "verified"
    # The F1 address survives as the lower-ranked hypothesis rather than being
    # deleted: it was never disproved, only outranked.
    assert {c.email for c in result.ranked()} == {
        "j.doe@alpinecars.com", "jane.doe@alpinef1.com"
    }


def test_the_summary_reports_every_address(two_domains, repos, settings, mx_ok):
    ctx, _ = two_domains
    steps = [DomainStep(), HarvestStep(), PatternStep(resolver=mx_ok)]
    result = E.enrich_candidate(
        ctx.candidate_id, repos, settings, searcher=ctx.searcher, steps=steps
    )
    summary = E.summary(result)
    assert len(summary["addresses"]) == 2
    assert {a["email"] for a in summary["addresses"]} == {
        "jane.doe@alpinef1.com", "j.doe@alpinecars.com"
    }


def test_it_all_still_costs_nothing(two_domains, repos, settings, mx_ok):
    ctx, _ = two_domains
    steps = [DomainStep(), HarvestStep(), PatternStep(resolver=mx_ok)]
    result = E.enrich_candidate(
        ctx.candidate_id, repos, settings, searcher=ctx.searcher, steps=steps
    )
    assert result.credits_spent == 0.0
