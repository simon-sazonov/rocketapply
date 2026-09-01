"""Finding a company's own site — `pitwall.enrich.discover`.

This is the module that decides what `companies.domain` and `company_domains`
contain, and everything downstream is built on top of that answer: a wrong
domain produces confident, plausible addresses aimed at a stranger's mail
server. So the tests here are mostly about what it *refuses*.

The scoring rules were all learned from real failures against the owner's own
49 companies, and each one has a test naming the failure it prevents.
"""

from __future__ import annotations

import pytest

from pitwall.enrich.discover import (
    DEFAULT_ENGINES,
    MAX_COMPANY_DOMAINS,
    NoUsableEngine,
    apply_domains,
    coverage_of,
    discover_domains,
    home_tld,
    is_motorsport,
    is_shop,
    looks_retail,
    name_forms,
    precision_of,
    probe_engines,
    queries_for,
    resolve,
    usable,
)
from tests.conftest import StubSearcher, serp


def company(name="Alpine", **kwargs):
    return {"id": 1, "name": name, "aliases": [], "series": "WEC",
            "country": "France", "domain": None, **kwargs}


# ─────────────────────────── the motorsport gate ─────────────────────────


def test_a_page_about_motorsport_is_recognised():
    assert is_motorsport("PREMA Racing — official site") is True
    assert is_motorsport("AF Corse, scuderia partner Ferrari") is True
    assert is_motorsport("Hypercar entry for Le Mans") is True
    assert is_motorsport("Rennteam Herberth") is True


def test_a_page_about_something_else_is_not():
    assert is_motorsport("Century Technology — AI for schools") is False
    assert is_motorsport("Campos Coffee | Specialty roasters") is False
    assert is_motorsport("Proton Mail: secure email") is False


def test_only_motorsport_pages_can_supply_a_domain(enrich_ctx, repos):
    """The rule that makes the tail of the list usable. "Century Motorsport",
    "Optimum Motorsport", "Trident" and "Garage 59" each share their
    distinctive word with a bigger business, and without this the resolver
    returned century.tech, optimum.com, proton.me and a coffee roaster."""
    searcher = StubSearcher({"Century Motorsport": [
        serp("https://century.tech/", "CENTURY — AI for education",
             "The world's leading AI teaching platform"),
    ]})
    result = resolve(repos, searcher, company("Century Motorsport"), check_mx=False)

    assert result.chosen is None
    assert any("none on a motorsport page" in w for w in result.warnings)


def test_a_motorsport_page_does_supply_one(repos):
    searcher = StubSearcher({"Century Motorsport": [
        serp("https://centurymotorsport.com/", "Century Motorsport",
             "GT3 and GT4 racing team"),
    ]})
    result = resolve(repos, searcher, company("Century Motorsport"), check_mx=False)
    assert result.chosen.domain == "centurymotorsport.com"


# ──────────────────────── coverage, precision, TLD ───────────────────────


def test_coverage_is_how_much_of_the_name_the_domain_carries():
    assert coverage_of("afcorse.it", ["AF Corse"]) == 1.0
    assert coverage_of("toyota.co.uk", ["Toyota Gazoo Racing"]) == pytest.approx(1 / 3)


def test_precision_is_how_much_of_the_domain_is_the_name():
    """The counterweight. Coverage alone let `afcorseusa.com` beat `afcorse.it`
    and `toyotagazooracingb2bshop.com` beat everything — each contains the
    whole company name and then some."""
    assert precision_of("afcorse.it", ["AF Corse"]) > precision_of(
        "afcorseusa.com", ["AF Corse"]
    )


def test_an_alias_can_supply_the_match_the_name_cannot():
    """`companies.csv` already records "TGR-E|Toyota Gazoo Racing Europe", and
    that is better information than any query this could invent."""
    forms = name_forms(company("Toyota Gazoo Racing", aliases=["TGR-E"]))
    assert "TGR-E" in forms
    assert coverage_of("tgr-e.com", forms) == 1.0


def test_the_hq_country_settles_a_tie():
    assert home_tld("afcorse.it", "Italy") is True
    assert home_tld("afcorseusa.com", "Italy") is False
    assert home_tld("tfsport.co.uk", "UK") is True
    assert home_tld("anything.com", None) is False


def test_the_country_beats_a_satellite_office(repos):
    searcher = StubSearcher({"AF Corse": [
        serp("https://afcorseusa.com/", "AF Corse USA — official", "GT racing"),
        serp("https://afcorse.it/", "AF Corse — sito ufficiale", "scuderia GT"),
    ]})
    result = resolve(
        repos, searcher, company("AF Corse", country="Italy", series="WEC/GT"),
        check_mx=False,
    )
    assert result.chosen.domain == "afcorse.it"


# ──────────────────────── what is never a domain ─────────────────────────


def test_a_merchandise_shop_is_excluded_outright():
    """It matches the full team name perfectly and is never where staff email
    lives, so it is excluded rather than merely penalised."""
    assert is_shop("toyotagazooracingb2bshop.com") is True
    assert usable("toyotagazooracingb2bshop.com") is False
    assert is_shop("premaracing.com") is False


def test_a_retail_arm_is_penalised_not_excluded():
    """`alpine-cars.co.uk` is Alpine's UK sales site. A car maker's own domain
    reasonably contains "cars", so this is a penalty the official-site marker
    can waive, not a ban."""
    assert looks_retail("alpine-cars.co.uk", "Alpine Endurance Team") is True
    assert usable("alpine-cars.co.uk") is True


def test_a_brands_other_web_properties_are_excluded():
    """A works team runs several *web properties* as well as several
    organisations, and nobody's staff mail lives on a parts catalogue or an
    anniversary microsite. Manthey resolved to `manthey-kit.com` and
    `manthey-25years.com` ahead of `manthey-racing.com` until this existed."""
    assert usable("manthey-kit.com") is False
    assert usable("manthey-25years.com") is False
    assert usable("porsche-experience.com") is False
    assert usable("manthey-racing.com") is True


def test_naming_the_team_is_a_signal(repos):
    """The team is the employer, which is the only thing being asked about."""
    from pitwall.enrich.discover import has_team_word

    assert has_team_word("manthey-racing.com", "Manthey") is True
    assert has_team_word("manthey.de", "Manthey") is False
    # …but it carries no information when the company name already says it.
    assert has_team_word("premaracing.com", "PREMA Racing") is False


def test_the_racing_arm_beats_the_parent_brand(repos):
    searcher = StubSearcher({"Manthey": [
        serp("https://manthey.de/", "Manthey", "Engineering group"),
        serp("https://manthey-racing.com/", "Manthey Racing",
             "Porsche GT3 racing team, Nürburgring"),
    ]})
    result = resolve(repos, searcher, company("Manthey", country="Germany"),
                     check_mx=False)
    assert result.chosen.domain == "manthey-racing.com"


def test_platforms_and_news_sites_are_never_a_company_domain():
    for domain in ("linkedin.com", "wikipedia.org", "motorsport.com",
                   "autosport.com", "greenhouse.io", "the-race.com"):
        assert usable(domain) is False


# ───────────────────────── several organisations ─────────────────────────


def test_a_works_team_keeps_both_of_its_domains(repos):
    """A profile that says "Alpine" does not say which organisation."""
    searcher = StubSearcher({"Alpine": [
        serp("https://alpinef1.com/", "Alpine F1 Team — official",
             "Formula 1 team based at Enstone"),
        serp("https://alpinecars.com/", "Official Home of Alpine Cars & Racing",
             "Alpine endurance racing and sports cars"),
    ]})
    result = resolve(repos, searcher, company("Alpine", country="France"),
                     check_mx=False)

    assert len(result.domains) >= 2
    assert set(result.domains) >= {"alpinef1.com", "alpinecars.com"}


def test_a_runner_up_that_is_barely_the_name_is_not_a_second_domain(repos):
    """"ART Grand Prix" reduces to the token `art` once the generic motorsport
    words are stripped, so an art shop covers 100% of it while being 25% name.
    Recording it would generate addresses at a shop."""
    searcher = StubSearcher({"ART Grand Prix": [
        serp("https://art-grandprix.com/", "ART Grand Prix — official",
             "Formula 2 and Formula 3 racing team"),
        serp("https://grandprixart.co.uk/", "Grand Prix Art — official",
             "Motorsport prints and racing artwork for sale"),
    ]})
    result = resolve(repos, searcher, company("ART Grand Prix", country="France"),
                     check_mx=False)
    assert result.chosen.domain == "art-grandprix.com"
    assert "grandprixart.co.uk" not in result.domains


def test_the_number_of_domains_is_capped(repos):
    hits = [
        serp(f"https://alpine{i}.com/", f"Alpine {i} — official racing", "GT racing")
        for i in range(6)
    ]
    searcher = StubSearcher({"Alpine": hits})
    result = resolve(repos, searcher, company("Alpine"), check_mx=False)
    assert len(result.domains) <= MAX_COMPANY_DOMAINS


# ───────────────────────────── the queries ───────────────────────────────


def test_every_query_is_quoted_or_uses_an_operator():
    """Without quotes these return the open web rather than the company."""
    for query in queries_for(company("Alpine", aliases=["Signatech Alpine"])):
        assert '"' in query


def test_the_series_qualifies_the_query():
    """"Alpine" is ambiguous; "Alpine WEC" is not — and the series is already
    in companies.csv."""
    assert any("WEC" in q for q in queries_for(company("Alpine", series="WEC")))


def test_a_company_with_no_series_still_gets_queries():
    queries = queries_for(company("Alpine", series=""))
    assert queries
    assert all("{series}" not in q for q in queries)


# ───────────────────────────── engine probing ────────────────────────────


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _searxng(monkeypatch, answers):
    """Stub SearXNG so no test touches the network."""
    def fake_get(url, params=None, timeout=None):
        return FakeResponse(answers.get((params or {}).get("engines"), {"results": []}))

    monkeypatch.setattr("httpx.get", fake_get)


def test_a_healthy_engine_that_honours_quotes_is_used(settings, monkeypatch):
    _searxng(monkeypatch, {
        "google": {"results": [{"url": "https://ferrari.com/", "title": "Ferrari"}]},
    })
    good, bad = probe_engines(settings, ["google"])
    assert good == ["google"]
    assert bad == []


def test_a_suspended_engine_is_skipped_with_its_reason(settings, monkeypatch):
    _searxng(monkeypatch, {
        "qwant": {"results": [], "unresponsive_engines": [["qwant", "CAPTCHA"]]},
    })
    good, bad = probe_engines(settings, ["qwant"])
    assert good == []
    assert bad == [("qwant", "CAPTCHA")]


def test_an_engine_that_ignores_quotes_is_rejected(settings, monkeypatch):
    """Bing answers every time and is the worst of them: it reads `"Iron Lynx"`
    as a question about the element. A liveness check cannot see that."""
    _searxng(monkeypatch, {
        "bing": {"results": [{"url": "https://example.com/", "title": "Something else"}]},
    })
    good, bad = probe_engines(settings, ["bing"])
    assert good == []
    assert bad == [("bing", "ignores quoted phrases")]


def test_an_engine_returning_nothing_is_rejected(settings, monkeypatch):
    _searxng(monkeypatch, {"mojeek": {"results": []}})
    assert probe_engines(settings, ["mojeek"]) == ([], [("mojeek", "no results")])


def test_the_default_pool_lists_google_cse_separately():
    """SearXNG treats it as a different engine with its own rate limit, and it
    was the only thing still answering after a pass had suspended the rest."""
    assert "google cse" in DEFAULT_ENGINES
    assert "google," in DEFAULT_ENGINES


# ──────────────────────────── the whole run ──────────────────────────────


def test_no_usable_engine_is_an_error_not_a_guess(repos, settings, monkeypatch):
    """Producing a best-of-a-bad-set answer with no working search behind it
    would be worse than saying so."""
    _searxng(monkeypatch, {})
    with pytest.raises(NoUsableEngine):
        discover_domains(repos, [company()], settings)


def test_apply_writes_the_primary_and_every_domain(repos, settings, project):
    from pitwall.enrich.discover import Candidate, Result

    repos.conn.execute("INSERT INTO companies (name) VALUES ('McLaren Racing')")
    result = Result(name="McLaren Racing", series="F1", current=None)
    result.chosen = Candidate("mclaren.com")
    result.siblings = [Candidate("mclarenracing.com")]

    written, recorded = apply_domains(repos, [result])
    assert written == 1
    assert recorded == 2

    stored = [d["domain"] for d in repos.domains.for_company(1)]
    assert set(stored) == {"mclaren.com", "mclarenracing.com"}
    # The CSV holds only the primary — it has one `domain` column.
    assert "mclaren.com" in (project / "config" / "companies.csv").read_text()


def test_apply_honours_an_explicit_choice(repos, settings, project):
    """The Setup page's tick boxes are the reason this takes results rather
    than doing its own resolution: the owner overrules the ranking."""
    from pitwall.enrich.discover import Candidate, Result

    repos.conn.execute("INSERT INTO companies (name) VALUES ('McLaren Racing')")
    result = Result(name="McLaren Racing", series="F1", current=None)
    result.chosen = Candidate("wrong.com")
    result.siblings = [Candidate("alsowrong.com")]

    apply_domains(repos, [result], chosen={"McLaren Racing": ["mclaren.com"]})
    assert [d["domain"] for d in repos.domains.for_company(1)] == ["mclaren.com"]


def test_apply_refuses_to_rewrite_a_csv_it_would_damage(repos, settings, project):
    """`save_companies` rewrites the whole file from the rows that *parsed*, so
    saving over a file with rejected rows in it would silently delete them.

    The second row here is skipped on load. Rewriting would drop it for good,
    and the owner would have no way of knowing which row went missing."""
    from pitwall.enrich.discover import Candidate, Result

    csv = project / "config" / "companies.csv"
    csv.write_text("name,tier\nMcLaren Racing,1\n,2\n", encoding="utf-8")
    repos.conn.execute("INSERT INTO companies (name) VALUES ('McLaren Racing')")
    result = Result(name="McLaren Racing", series="F1", current=None)
    result.chosen = Candidate("mclaren.com")

    written, recorded = apply_domains(repos, [result])
    assert written == 0                       # the CSV is left alone
    assert ",2" in csv.read_text()            # …including the row it could not read
    assert recorded == 1                      # the database still learns the domain


def test_a_result_reports_whether_the_search_actually_worked(repos):
    """An engine that dies mid-run answers nothing, forever. Counting empty
    searches is the only way to notice before the whole pass is spent."""
    dead = StubSearcher({})
    result = resolve(repos, dead, company(), check_mx=False)
    assert result.searches_run > 0
    assert result.search_worked is False
