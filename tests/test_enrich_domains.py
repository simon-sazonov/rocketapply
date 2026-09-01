"""Domain resolution — §2.1.

The tests that matter here are the negative ones. A wrong domain poisons every
address derived from it, and the two ways to get one wrong are an ATS host
mistaken for a company (`boards.greenhouse.io`) and a same-name organisation
mistaken for the right one (a car dealership for a race team). The first must
never happen; the second cannot be prevented by any string test, so what is
asserted instead is that the owner's correction wins for good.
"""

from __future__ import annotations

import pytest

from pitwall.enrich.domains import (
    DomainStep,
    domain_from_url,
    domain_label,
    domain_matches_company,
    is_company_domain,
    registrable_domain,
    resolve_domain,
    set_manual_domain,
)
from tests.conftest import StubSearcher, serp

# ─────────────────────── registrable domain parsing ──────────────────────


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("alpine.com", "alpine.com"),
        ("www.alpine.com", "alpine.com"),
        ("careers.alpine.com", "alpine.com"),
        ("jobs.alpine.co.uk", "alpine.co.uk"),
        ("www.prema.org.uk", "prema.org.uk"),
        ("ALPINE.COM", "alpine.com"),
        ("alpine.com.", "alpine.com"),
        ("alpine.com:443", "alpine.com"),
        ("shop.store.alpine.com", "alpine.com"),
        ("news.alpine.com.au", "alpine.com.au"),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


@pytest.mark.parametrize("host", ["", "localhost", "alpine", "192.168.1.1", "  ", None])
def test_unusable_hosts_return_none(host):
    assert registrable_domain(host) is None


def test_domain_from_url_handles_a_bare_host():
    assert domain_from_url("https://careers.alpine.com/jobs/12") == "alpine.com"
    assert domain_from_url("alpine.com/careers") == "alpine.com"
    assert domain_from_url("") is None


def test_domain_label_is_the_name_bearing_part():
    assert domain_label("alpine-racing.co.uk") == "alpineracing"
    assert domain_label("alpine.com") == "alpine"


# ───────────────────────── what is not a company ─────────────────────────


@pytest.mark.parametrize(
    "domain",
    ["greenhouse.io", "lever.co", "myworkdayjobs.com", "linkedin.com",
     "wikipedia.org", "indeed.com", "github.com", "hunter.io", "youtube.com"],
)
def test_platforms_are_never_a_company_domain(domain):
    assert is_company_domain(domain) is False


def test_a_careers_url_on_an_ats_is_not_the_company_domain():
    """The single most likely way to derive addresses at the wrong mail server."""
    assert is_company_domain(domain_from_url("https://boards.greenhouse.io/alpine")) is False
    assert is_company_domain(domain_from_url("https://jobs.lever.co/alpine/123")) is False


def test_a_real_company_domain_passes():
    assert is_company_domain("alpine-racing.com") is True
    assert is_company_domain(None) is False


# ──────────────────────── does the name match? ───────────────────────────


def test_a_domain_matches_its_company():
    assert domain_matches_company("prema.org.uk", "PREMA Racing") is True
    assert domain_matches_company("alpine-racing.com", "Alpine Endurance Team") is True
    assert domain_matches_company("mclaren.com", "McLaren Racing") is True


def test_an_unrelated_domain_does_not_match():
    assert domain_matches_company("bhf.org.uk", "PREMA Racing") is False
    assert domain_matches_company("cks.nice.org.uk", "AF Corse") is False


def test_matching_is_generous_and_the_owner_is_the_judge():
    """`alpine-cars.co.uk` is a dealership, not the race team — and no string
    test can tell them apart. The safeguard is the override, not the matcher."""
    assert domain_matches_company("alpine-cars.co.uk", "Alpine Endurance Team") is True


def test_matching_with_an_empty_name():
    assert domain_matches_company("alpine.com", "") is False


# ──────────────────────── the resolution order ───────────────────────────


def test_the_override_wins_over_every_automatic_source(enrich_ctx, repos):
    """§2.1 says "let the owner correct it", and a correction that an automatic
    source can outvote is not a correction — so the override is checked first,
    departing from the brief's literal ordering. See the module docstring."""
    ctx = enrich_ctx()
    company_id = int(ctx.company["id"])
    repos.conn.execute(
        "UPDATE companies SET domain = 'wrong-guess.com' WHERE id = ?", (company_id,)
    )
    ctx.company = dict(
        repos.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    )
    set_manual_domain(repos, company_id, "alpine-racing.com")

    resolved = resolve_domain(ctx, company_id, "Alpine")
    assert resolved.domain == "alpine-racing.com"
    assert resolved.source == "manual"


def test_the_config_domain_column_is_used(enrich_ctx, repos):
    ctx = enrich_ctx()
    company_id = int(ctx.company["id"])
    repos.conn.execute(
        "UPDATE companies SET domain = 'alpine.com' WHERE id = ?", (company_id,)
    )
    ctx.company = dict(
        repos.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    )
    resolved = resolve_domain(ctx, company_id, "Alpine")
    assert (resolved.domain, resolved.source) == ("alpine.com", "config")


def test_a_careers_url_supplies_the_domain(enrich_ctx, repos):
    ctx = enrich_ctx()
    company_id = int(ctx.company["id"])
    repos.conn.execute(
        "UPDATE companies SET careers_url = 'https://careers.alpine.com/jobs' WHERE id = ?",
        (company_id,),
    )
    ctx.company = dict(
        repos.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    )
    resolved = resolve_domain(ctx, company_id, "Alpine")
    assert (resolved.domain, resolved.source) == ("alpine.com", "careers_url")


def test_an_ats_careers_url_is_skipped_not_used(enrich_ctx, repos):
    """A Greenhouse careers_url is normal and not an error; it just cannot
    answer this question, and must not be allowed to answer it wrongly."""
    ctx = enrich_ctx(searcher=StubSearcher())
    company_id = int(ctx.company["id"])
    repos.conn.execute(
        "UPDATE companies SET careers_url = 'https://boards.greenhouse.io/alpine' WHERE id = ?",
        (company_id,),
    )
    ctx.company = dict(
        repos.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    )
    resolved = resolve_domain(ctx, company_id, "Alpine")
    assert resolved.domain != "greenhouse.io"
    assert resolved.domain is None


def test_stored_serp_rows_resolve_the_domain_for_free(enrich_ctx, repos):
    """The cheapest source there is: pages this database already fetched."""
    ctx = enrich_ctx()
    company_id = int(ctx.company["id"])
    repos.conn.execute(
        "INSERT INTO queries (company_id, template_id, query_text, query_hash)"
        " VALUES (?, 't1', 'q', 'h1')", (company_id,)
    )
    qid = int(repos.conn.execute("SELECT id FROM queries").fetchone()["id"])
    for i, url in enumerate([
        "https://www.linkedin.com/in/someone",
        "https://www.alpine-racing.com/team",
        "https://www.alpine-racing.com/news",
        "https://en.wikipedia.org/wiki/Alpine",
    ]):
        repos.conn.execute(
            "INSERT INTO search_results (query_id, provider, rank, url, canonical_url)"
            " VALUES (?, 'searxng', ?, ?, ?)", (qid, i, url, url)
        )

    resolved = resolve_domain(ctx, company_id, "Alpine")
    assert (resolved.domain, resolved.source) == ("alpine-racing.com", "serp")
    assert "2 stored search result" in (resolved.detail or "")


def test_serp_resolution_ignores_social_and_reference_hosts(enrich_ctx, repos):
    ctx = enrich_ctx(searcher=StubSearcher())
    company_id = int(ctx.company["id"])
    repos.conn.execute(
        "INSERT INTO queries (company_id, template_id, query_text, query_hash)"
        " VALUES (?, 't1', 'q', 'h1')", (company_id,)
    )
    qid = int(repos.conn.execute("SELECT id FROM queries").fetchone()["id"])
    for i in range(5):
        url = f"https://www.linkedin.com/in/person-{i}"
        repos.conn.execute(
            "INSERT INTO search_results (query_id, provider, rank, url, canonical_url)"
            " VALUES (?, 'searxng', ?, ?, ?)", (qid, i, url, url)
        )
    assert resolve_domain(ctx, company_id, "Alpine").domain is None


def test_a_search_resolves_the_domain_when_nothing_else_can(enrich_ctx):
    searcher = StubSearcher({
        "official site": [
            serp("https://en.wikipedia.org/wiki/Alpine"),
            serp("https://www.alpine-racing.com/"),
        ]
    })
    ctx = enrich_ctx(searcher=searcher)
    resolved = resolve_domain(ctx, int(ctx.company["id"]), "Alpine")
    assert (resolved.domain, resolved.source) == ("alpine-racing.com", "search")
    assert searcher.queries == ['"Alpine" official site']


def test_a_failing_search_is_a_state_not_an_exception(enrich_ctx):
    searcher = StubSearcher()
    searcher.fail_on = {"official site"}
    ctx = enrich_ctx(searcher=searcher)
    resolved = resolve_domain(ctx, int(ctx.company["id"]), "Alpine")
    assert resolved.domain is None
    assert "search failed" in (resolved.detail or "")


def test_no_searcher_at_all_is_survivable(enrich_ctx):
    ctx = enrich_ctx(searcher=None)
    resolved = resolve_domain(ctx, int(ctx.company["id"]), "Alpine")
    assert resolved.domain is None


def test_alternatives_are_offered_for_a_one_click_correction(enrich_ctx):
    searcher = StubSearcher({
        "official site": [
            serp("https://www.somethingelse.com/"),
            serp("https://www.anotherthing.org/"),
        ]
    })
    ctx = enrich_ctx(searcher=searcher)
    resolved = resolve_domain(ctx, int(ctx.company["id"]), "Alpine")
    assert resolved.domain is None
    assert "somethingelse.com" in resolved.alternatives


# ─────────────────────────────── the step ────────────────────────────────


def test_the_step_caches_the_domain_on_the_company(enrich_ctx, repos):
    searcher = StubSearcher({"official site": [serp("https://www.alpine-racing.com/")]})
    ctx = enrich_ctx(searcher=searcher)
    outcome = DomainStep().run(ctx)

    assert outcome.outcome == "ok"
    assert ctx.domain == "alpine-racing.com"
    assert ctx.company_domain_id is not None
    stored = repos.domains.preferred(int(ctx.company["id"]))
    assert stored["domain"] == "alpine-racing.com"
    assert stored["source"] == "search"


def test_the_step_is_free_and_says_so():
    assert DomainStep.spends_credits is False


def test_the_step_skips_a_person_with_no_employer(enrich_ctx, repos):
    ctx = enrich_ctx(company_name=None, searcher=StubSearcher())
    ctx.candidate = {**ctx.candidate, "current_company": None}
    ctx.company = None
    outcome = DomainStep().run(ctx)
    assert outcome.outcome == "skipped"
    assert ctx.domain is None


def test_the_step_reports_empty_when_nothing_resolves(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher())
    outcome = DomainStep().run(ctx)
    assert outcome.outcome == "empty"


def test_set_manual_domain_normalises_what_the_owner_types(enrich_ctx, repos):
    ctx = enrich_ctx()
    company_id = int(ctx.company["id"])
    assert set_manual_domain(repos, company_id, "https://www.Alpine-Racing.com/careers") == (
        "alpine-racing.com"
    )
    with pytest.raises(ValueError):
        set_manual_domain(repos, company_id, "not a domain")


def test_the_candidates_own_employer_wins_over_the_search_target(enrich_ctx):
    """The address format follows the employer, not the query that found them."""
    ctx = enrich_ctx(name="Jane Doe", company_name="Alpine")
    ctx.candidate = {**ctx.candidate, "current_company": "Alpine Racing UK"}
    assert ctx.company_name == "Alpine Racing UK"
