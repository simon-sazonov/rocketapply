"""Job source adapters, against saved responses — §2.3 acceptance criteria.

No network. Every adapter is exercised against a fixture in
`tests/fixtures/jobs/`, four of which are verbatim captures from live boards
(Greenhouse, Lever, Ashby, SmartRecruiters, 30 Aug 2026) and two of which are
shaped from the vendors' published APIs because I could not find a public token.

Fixtures are the point, not a convenience: aggregator HTML and ATS JSON change
without warning, and a fixture test is what tells you the parser broke rather
than the source being empty.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from pitwall.sources.jobs import (
    ATS_KINDS,
    ENDPOINTS,
    AggregatorSource,
    ATSSource,
    CareerPageSource,
    JobCache,
    NotFound,
    PoliteClient,
    RobotsDisallowed,
    RobotsGate,
    SourceError,
    build_sources,
    endpoint_for,
    load_source_config,
)
from pitwall.sources.jobs.aggregator import parse_job_postings, parse_sitemap
from pitwall.sources.jobs.ats import parse
from pitwall.sources.jobs.base import JobPosting, iso_date, strip_html

FIXTURES = Path(__file__).parent / "fixtures" / "jobs"
ROOT = Path(__file__).resolve().parents[1]


def fixture(name: str):
    xml = FIXTURES / f"ats_{name}.xml"
    if xml.exists():        # Personio and Teamtailor answer in XML
        return xml.read_text(encoding="utf-8")
    return json.loads((FIXTURES / f"ats_{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


def stub_http(monkeypatch, routes: dict[str, FakeResponse | Exception]):
    """Route by URL substring; anything unrouted is a 404."""
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        for needle, response in routes.items():
            if needle in url:
                if isinstance(response, Exception):
                    raise response
                return response
        return FakeResponse("", 404)

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


@pytest.fixture()
def client(settings, tmp_path):
    settings.jobs_crawl_delay = 0.0
    return PoliteClient(settings, cache=JobCache(root=tmp_path / "jobcache", ttl_days=0))


# ───────────────────────── every ATS has a fixture ───────────────────────


@pytest.mark.parametrize("kind", sorted(ATS_KINDS))
def test_every_ats_adapter_has_a_fixture_and_parses_it(kind):
    """§2.3: "Test every adapter against a saved response fixture"."""
    postings = parse(kind, fixture(kind), "example")
    assert postings, f"{kind} produced nothing from its fixture"
    for job in postings:
        assert job.title and job.url
        assert job.url.startswith("http")


@pytest.mark.parametrize("kind", sorted(ATS_KINDS))
def test_every_ats_endpoint_pattern_is_a_url_with_a_token(kind):
    assert "{token}" in ENDPOINTS[kind]
    assert endpoint_for(kind, "abc").startswith("https://")
    assert "abc" in endpoint_for(kind, "abc")


def test_an_unknown_ats_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="unknown ATS"):
        endpoint_for("taleo", "x")
    with pytest.raises(ValueError, match="unknown ATS"):
        ATSSource("taleo", "x")


def test_greenhouse_reads_the_nested_location_and_the_absolute_url():
    job = parse("greenhouse", fixture("greenhouse"))[0]
    assert job.location, "location.name is nested one level down"
    assert "gh_jid" in job.url or job.url.startswith("http")
    assert job.external_id


def test_lever_reads_the_title_from_text_and_the_date_from_epoch_millis():
    """Lever agrees with nobody: the title is `text`, the date is ms since 1970."""
    job = parse("lever", fixture("lever"))[0]
    assert job.title and job.title != "text"
    assert job.posted_at and job.posted_at.startswith("2")
    assert job.employment_type


def test_ashby_skips_unlisted_postings():
    data = fixture("ashby")
    data["jobs"][0]["isListed"] = False
    kept = parse("ashby", data)
    assert len(kept) == len(data["jobs"]) - 1


def test_smartrecruiters_builds_the_public_url_from_the_company_and_id():
    job = parse("smartrecruiters", fixture("smartrecruiters"))[0]
    assert job.url.startswith("https://jobs.smartrecruiters.com/")
    assert job.external_id in job.url


def test_workable_orders_the_location_city_first():
    job = parse("workable", fixture("workable"))[0]
    assert job.location.startswith("Silverstone")


def test_recruitee_reads_the_public_careers_url():
    job = parse("recruitee", fixture("recruitee"))[0]
    assert "recruitee.com/o/" in job.url
    assert job.location == "Vicenza, Veneto, Italy"


def test_a_posting_without_a_title_or_url_is_dropped_not_half_stored():
    assert parse("greenhouse", {"jobs": [{"id": 1}]}) == []
    assert parse("lever", [{"id": "x"}]) == []
    with pytest.raises(ValueError):
        JobPosting(title="", url="https://x.test")


def test_the_ats_adapter_calls_the_endpoint_it_advertises(settings, client, monkeypatch):
    stub_http(monkeypatch, {
        "boards-api.greenhouse.io": FakeResponse(json.dumps(fixture("greenhouse")))
    })
    source = ATSSource("greenhouse", "apex", settings=settings, client=client)
    jobs = source.fetch()
    assert source.name == "greenhouse:apex"
    assert source.kind == "ats_json"
    assert len(jobs) == len(fixture("greenhouse")["jobs"])


def test_probing_a_wrong_token_answers_none_rather_than_raising(settings, client, monkeypatch):
    """A 404 is the ordinary answer for a guess — discover_ats.py makes hundreds."""
    stub_http(monkeypatch, {})
    assert ATSSource("lever", "nope", settings=settings, client=client).probe() is None


def test_a_404_is_told_apart_from_an_outage(settings, client, monkeypatch):
    """`discover_ats.py --verify` clears a board that 404s, never one that is down."""
    stub_http(monkeypatch, {"api.lever.co/v0/postings/down": FakeResponse("", 503)})
    with pytest.raises(SourceError) as down:
        ATSSource("lever", "down", settings=settings, client=client).fetch()
    assert not isinstance(down.value, NotFound)
    with pytest.raises(NotFound):
        ATSSource("lever", "gone", settings=settings, client=client).fetch()


# ─────────── ATS discovery for the people-search list (companies) ─────────


def _discover_ats(monkeypatch):
    """scripts/discover_ats.py as a module, with its console captured."""
    from rich.console import Console

    spec = importlib.util.spec_from_file_location(
        "discover_ats", ROOT / "scripts" / "discover_ats.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = io.StringIO()
    monkeypatch.setattr(mod, "console", Console(file=out, width=240))
    return mod, out


def _boards(routes):
    """`fetch(kind, token)` over a dict of boards; anything unrouted is a 404."""
    def fetch(kind, token):
        answer = routes.get((kind, token), NotFound(f"{kind}:{token}: not found (404)"))
        if isinstance(answer, Exception):
            raise answer
        return answer
    return fetch


def _posts(*pairs):
    return [JobPosting(title=t, url=f"https://b.test/{i}", company_name=c)
            for i, (t, c) in enumerate(pairs)]


def _company(repos, name, aliases=(), kind=None, token=None):
    repos.conn.execute(
        "INSERT INTO companies (name, aliases, ats_kind, ats_token) VALUES (?,?,?,?)",
        (name, json.dumps(list(aliases)), kind, token),
    )


def test_people_search_discovery_passes_over_a_strangers_board(repos, tmp_path, monkeypatch):
    """The first board to answer is a sample account, the next is a hospital
    network that shares the name; the one adopted names the team."""
    mod, out = _discover_ats(monkeypatch)
    _company(repos, "McLaren Racing", ["McLaren F1", "McLaren Applied", "MTC"])
    fetch = _boards({
        ("recruitee", "mclarenracing"): _posts(("Senior Marketer (Sample)", "Fiture Marketing")),
        ("greenhouse", "mclaren"): _posts(("Ward Nurse", "McLaren Health Care")),
        ("greenhouse", "mclarenf1"): _posts(("Race Strategy Analyst", "McLaren F1")),
    })
    store = mod.BoardStore(repos.conn, tmp_path / "backups")

    hits = mod.discover_companies(store, repos.companies.all(), ["recruitee", "greenhouse"],
                                  4, fetch)

    assert hits == [("McLaren Racing", "greenhouse", "mclarenf1", 1)]
    row = repos.companies.by_name("McLaren Racing")
    assert (row["ats_kind"], row["ats_token"]) == ("greenhouse", "mclarenf1")
    text = out.getvalue()
    assert "rejected recruitee:mclarenracing for McLaren Racing: a placeholder board" in text
    assert ("rejected greenhouse:mclaren for McLaren Racing: "
            "the postings name mclaren health care") in text
    # the backup was taken before the write, so it has no board in it yet
    assert store.backup is not None and store.backup.parent == tmp_path / "backups"
    snapshot = sqlite3.connect(store.backup)
    try:
        assert snapshot.execute("SELECT ats_kind FROM companies").fetchall() == [(None,)]
    finally:
        snapshot.close()


def test_people_search_discovery_adopts_nothing_it_cannot_verify(repos, tmp_path, monkeypatch):
    """On boards that name nobody: `prema` is a fragment of "PREMA Racing", and
    `genesis` is only an alias of Genesis Magma Racing — the live board there
    is Genesis Global, a capital-markets software firm."""
    mod, out = _discover_ats(monkeypatch)
    _company(repos, "PREMA Racing", ["PREMA Powerteam"])
    _company(repos, "Genesis Magma Racing", ["Genesis", "Hyundai Motorsport"])
    fetch = _boards({
        ("lever", "prema"): _posts(("Client Partner", None)),
        ("lever", "genesis"): _posts(("Head of DevOps", None)),
    })
    store = mod.BoardStore(repos.conn, tmp_path / "backups")

    assert mod.discover_companies(store, repos.companies.all(), ["lever"], 4, fetch) == []
    assert not any(r["ats_kind"] for r in repos.companies.all())
    assert "'genesis' is only an alias" in out.getvalue()
    assert store.backup is None, "nothing was written, so nothing needed backing up"


def test_a_dry_run_records_nothing_and_takes_no_backup(repos, tmp_path, monkeypatch):
    mod, _ = _discover_ats(monkeypatch)
    _company(repos, "Manthey", ["Manthey Racing"])
    fetch = _boards({("lever", "manthey"): _posts(("Data Engineer", None))})
    store = mod.BoardStore(repos.conn, tmp_path / "backups", dry_run=True)

    hits = mod.discover_companies(store, repos.companies.all(), ["lever"], 4, fetch)

    assert [h[:3] for h in hits] == [("Manthey", "lever", "manthey")]
    assert repos.companies.by_name("Manthey")["ats_kind"] is None
    assert store.backup is None and not (tmp_path / "backups").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_verify_clears_only_the_boards_shown_to_be_wrong(repos, tmp_path, monkeypatch, dry_run):
    mod, out = _discover_ats(monkeypatch)
    recorded = {
        # name: (aliases, kind, token, what the board answers)
        "McLaren Racing": (["McLaren F1"], "greenhouse", "mclarenf1",
                           _posts(("Strategy Analyst", "McLaren F1"))),
        "PREMA Racing": (["PREMA Powerteam"], "lever", "prema",
                         _posts(("Client Partner", None))),
        "Genesis Magma Racing": (["Genesis", "Hyundai Motorsport"], "lever", "genesis",
                                 _posts(("Head of DevOps", None))),
        "Trident": ([], "recruitee", "trident", _posts(("Senior Marketer (Sample)", None))),
        "AF Corse": ([], "ashby", "afcorse", NotFound("ashby:afcorse: not found (404)")),
        "Kessel Racing": ([], "taleo", "kessel", ValueError("unknown ATS 'taleo'")),
        "Iron Lynx": ([], "workable", "ironlynx", SourceError("workable:ironlynx: HTTP 503")),
        "Garage 59": ([], "smartrecruiters", "garage59", []),
    }
    for name, (aliases, kind, token, _) in recorded.items():
        _company(repos, name, aliases, kind, token)
    fetch = _boards({(kind, token): answer for _, kind, token, answer in recorded.values()})
    store = mod.BoardStore(repos.conn, tmp_path / "backups", dry_run=dry_run)

    cleared = mod.verify_companies(store, repos.companies.all(), fetch)

    wrong = ["AF Corse", "Genesis Magma Racing", "Kessel Racing", "PREMA Racing", "Trident"]
    assert sorted(cleared) == wrong
    boards = {r["name"]: r["ats_token"] for r in repos.companies.all()}
    if dry_run:
        assert all(boards.values()) and store.backup is None
        return
    # a timeout or an empty board says nothing about whose board it is: kept
    assert {n for n, token in boards.items() if token} == {"McLaren Racing", "Iron Lynx",
                                                           "Garage 59"}
    assert store.backup is not None
    snapshot = sqlite3.connect(store.backup)
    try:
        before = snapshot.execute(
            "SELECT COUNT(*) FROM companies WHERE ats_kind IS NOT NULL").fetchone()[0]
    finally:
        snapshot.close()
    assert before == len(recorded)
    assert "the board is gone (404)" in out.getvalue()


def test_verify_from_the_command_line_backs_up_beside_the_database(settings, repos,
                                                                  monkeypatch):
    mod, _ = _discover_ats(monkeypatch)
    _company(repos, "Trident", [], "recruitee", "trident")
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(mod, "board_fetcher", lambda _settings: _boards({
        ("recruitee", "trident"): _posts(("Senior Marketer (Sample)", None)),
    }))

    mod.main(companies=None, limit_tokens=4, ats=None, show=False, clear=None,
             dry_run=False, targets=False, verify=True)

    assert repos.companies.by_name("Trident")["ats_kind"] is None
    backups = list((settings.db_file.parent / "backups").glob("pitwall-pre-ats-*.db"))
    assert len(backups) == 1


# ───────────────────────────── the aggregator ────────────────────────────


def test_the_sitemap_parser_reads_locs_and_lastmods():
    xml = (
        '<?xml version="1.0"?><urlset>'
        "<url><loc>https://x.test/en/job/a</loc><lastmod>2026-08-24T08:47Z</lastmod></url>"
        "<url><loc>https://x.test/about</loc></url></urlset>"
    )
    assert parse_sitemap(xml) == [
        ("https://x.test/en/job/a", "2026-08-24T08:47Z"),
        ("https://x.test/about", None),
    ]


def test_a_sitemap_index_without_url_blocks_still_yields_locs():
    xml = "<sitemapindex><sitemap><loc>https://x.test/s1.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(xml) == [("https://x.test/s1.xml", None)]


def test_job_posting_json_ld_is_read_from_a_real_captured_page():
    html = (FIXTURES / "aggregator_jobposting.html").read_text(encoding="utf-8")
    jobs = parse_job_postings(html, "https://example.test/job/x")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Engineering Manager - Brake Systems"
    assert job.company_name
    assert job.location and "," in job.location   # locality, region, country
    assert job.posted_at and job.posted_at.startswith("2026-")
    assert job.employment_type == "FULL_TIME"
    assert job.description and "<" not in job.description


def test_non_jobposting_structured_data_is_ignored():
    html = (FIXTURES / "aggregator_jobposting.html").read_text(encoding="utf-8")
    assert "WebSite" in html          # the fixture carries a decoy
    assert len(parse_job_postings(html, "u")) == 1


def test_a_page_with_no_structured_data_yields_nothing_rather_than_guessing():
    assert parse_job_postings("<html><body>Careers</body></html>", "u") == []


def test_unparseable_json_ld_does_not_take_the_page_down():
    html = '<script type="application/ld+json">{not json</script>'
    assert parse_job_postings(html, "u") == []


def test_the_aggregator_filters_the_sitemap_to_job_urls(settings, client, monkeypatch):
    sitemap = (
        "<urlset>"
        "<url><loc>https://x.test/job/a</loc><lastmod>2026-08-24</lastmod></url>"
        "<url><loc>https://x.test/job/b</loc><lastmod>2026-08-20</lastmod></url>"
        "<url><loc>https://x.test/about</loc></url></urlset>"
    )
    stub_http(monkeypatch, {"sitemap.xml": FakeResponse(sitemap), "robots.txt": FakeResponse("")})
    source = AggregatorSource("x", "https://x.test/sitemap.xml", settings=settings, client=client)
    urls = source.job_urls()
    assert [u for u, _ in urls] == ["https://x.test/job/a", "https://x.test/job/b"]
    assert urls[0][1] > urls[1][1], "newest first, so a capped run spends its budget well"


def test_the_aggregator_stops_when_a_whole_board_refuses(settings, client, monkeypatch):
    """A WAF should fail as a board, not as forty separate pages."""
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://x.test/job/{i}</loc></url>" for i in range(10)
    ) + "</urlset>"
    stub_http(monkeypatch, {
        "sitemap.xml": FakeResponse(sitemap),
        "robots.txt": FakeResponse(""),
        "/job/": FakeResponse("<html>nope</html>", 403),
    })
    source = AggregatorSource("x", "https://x.test/sitemap.xml", settings=settings, client=client)
    with pytest.raises(SourceError, match="403|failed"):
        source.fetch()


def test_the_detail_fetch_budget_is_respected(settings, client, monkeypatch):
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://x.test/job/{i}</loc></url>" for i in range(50)
    ) + "</urlset>"
    html = (FIXTURES / "aggregator_jobposting.html").read_text(encoding="utf-8")
    seen = stub_http(monkeypatch, {
        "sitemap.xml": FakeResponse(sitemap),
        "robots.txt": FakeResponse(""),
        "/job/": FakeResponse(html),
    })
    source = AggregatorSource(
        "x", "https://x.test/sitemap.xml", settings=settings, client=client, max_details=5
    )
    assert len(source.fetch()) == 5
    assert sum(1 for u in seen if "/job/" in u) == 5


# ──────────────────────────── the career page ────────────────────────────


def test_the_career_page_prefers_structured_data(settings, client, monkeypatch):
    html = (FIXTURES / "aggregator_jobposting.html").read_text(encoding="utf-8")
    stub_http(monkeypatch, {"careers": FakeResponse(html), "robots.txt": FakeResponse("")})
    source = CareerPageSource("careers:X", "https://x.test/careers", settings=settings, client=client)
    assert len(source.fetch()) == 1


def test_the_career_page_falls_back_to_an_injected_extractor(settings, client, monkeypatch):
    stub_http(monkeypatch, {
        "careers": FakeResponse("<html><body>We are hiring a Race Engineer</body></html>"),
        "robots.txt": FakeResponse(""),
    })
    called = {}

    def extractor(text, url):
        called["text"] = text
        return [JobPosting(title="Race Engineer", url=url)]

    source = CareerPageSource(
        "careers:X", "https://x.test/careers", settings=settings, client=client,
        llm_extractor=extractor,
    )
    jobs = source.fetch()
    assert [j.title for j in jobs] == ["Race Engineer"]
    assert "hiring" in called["text"]


def test_without_an_extractor_an_unstructured_page_yields_nothing(settings, client, monkeypatch):
    stub_http(monkeypatch, {"careers": FakeResponse("<html>nothing</html>"),
                            "robots.txt": FakeResponse("")})
    source = CareerPageSource("c", "https://x.test/careers", settings=settings, client=client)
    assert source.fetch() == []


# ───────────────────── robots.txt, enforced in code ──────────────────────


def test_robots_txt_is_parsed_and_applied(monkeypatch):
    """§2 acceptance: "enforced in code, not just documented"."""
    stub_http(monkeypatch, {
        "robots.txt": FakeResponse("User-agent: *\nDisallow: /private/\nAllow: /job/\n")
    })
    gate = RobotsGate("PitWall/2.0")
    assert gate.allows("https://x.test/job/a") is True
    assert gate.allows("https://x.test/private/a") is False


def test_a_disallowed_url_is_never_fetched(settings, client, monkeypatch):
    seen = stub_http(monkeypatch, {
        "robots.txt": FakeResponse("User-agent: *\nDisallow: /\n"),
        "x.test/job": FakeResponse("<html/>"),
    })
    client.robots.reset()
    with pytest.raises(RobotsDisallowed):
        client.get("https://x.test/job/a")
    assert not any("/job/a" in u for u in seen), "the request was never made"


def test_a_missing_robots_txt_means_allowed(monkeypatch):
    """RFC 9309: an unreachable robots.txt is not a refusal."""
    stub_http(monkeypatch, {"robots.txt": FakeResponse("", 404)})
    assert RobotsGate("PitWall/2.0").allows("https://x.test/job/a") is True


def test_an_unreachable_robots_txt_does_not_disable_the_source(monkeypatch):
    stub_http(monkeypatch, {"robots.txt": httpx.ConnectError("down")})
    assert RobotsGate("PitWall/2.0").allows("https://x.test/job/a") is True


def test_robots_txt_is_fetched_once_per_host(monkeypatch):
    seen = stub_http(monkeypatch, {"robots.txt": FakeResponse("User-agent: *\n")})
    gate = RobotsGate("PitWall/2.0")
    for _ in range(4):
        gate.allows("https://x.test/job/a")
    assert sum(1 for u in seen if "robots.txt" in u) == 1


def test_crawl_delay_is_honoured_when_the_site_asks_for_one(monkeypatch):
    stub_http(monkeypatch, {"robots.txt": FakeResponse("User-agent: *\nCrawl-delay: 7\n")})
    assert RobotsGate("PitWall/2.0").crawl_delay("https://x.test/a") == 7.0


# ──────────────────── identifying honestly, and 403 ──────────────────────


def test_the_user_agent_says_what_this_is(settings, client, monkeypatch):
    seen_headers = {}

    def fake_get(url, **kwargs):
        seen_headers.update(kwargs.get("headers") or {})
        return FakeResponse("{}")

    monkeypatch.setattr(httpx, "get", fake_get)
    client.robots.reset()
    client.get("https://x.test/a")
    assert "PitWall" in seen_headers["User-Agent"]
    assert "Mozilla" not in seen_headers["User-Agent"], "never impersonate a browser"


def test_a_403_is_reported_as_the_site_declining_this_client(settings, client, monkeypatch):
    stub_http(monkeypatch, {"robots.txt": FakeResponse(""), "x.test/a": FakeResponse("", 403)})
    client.robots.reset()
    with pytest.raises(SourceError) as exc:
        client.get("https://x.test/a")
    assert "403" in str(exc.value)
    assert "User-Agent" in str(exc.value)


# ─────────────────────────── the source registry ─────────────────────────


def test_the_shipped_config_disables_motorsportjobs_with_a_reason():
    """§2.1: the site 403s every honest client, so it stays off (see the file)."""
    rows = {r["name"]: r for r in load_source_config(ROOT / "config" / "job_sources.yaml")}
    entry = rows["motorsportjobs"]
    assert entry["enabled"] is False
    assert "403" in entry["reason"]


def test_disabled_sources_are_not_built(settings, tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        # quoted: YAML 1.1 reads a bare `on` as the boolean True
        "  - {name: 'live', kind: aggregator, sitemap_url: 'https://a.test/s.xml'}\n"
        "  - {name: 'dark', kind: aggregator, sitemap_url: 'https://b.test/s.xml',"
        " enabled: false}\n",
        encoding="utf-8",
    )
    names = [s.name for s in build_sources(settings, config_path=path)]
    assert names == ["live"]


def test_company_ats_columns_become_sources(settings):
    companies = [
        {"id": 1, "name": "Apex", "ats_kind": "greenhouse", "ats_token": "apex"},
        {"id": 2, "name": "No Board", "ats_kind": None, "ats_token": None},
    ]
    sources = build_sources(settings, companies, config_path=Path("/nonexistent.yaml"))
    assert [s.name for s in sources] == ["greenhouse:apex"]
    assert sources[0].company_id == 1


def test_the_enabled_kinds_gate_which_adapters_are_built(settings, tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - {name: agg, kind: aggregator, sitemap_url: 'https://a.test/s.xml'}\n"
        "  - {name: page, kind: career_page, url: 'https://b.test/careers'}\n",
        encoding="utf-8",
    )
    assert [s.name for s in build_sources(settings, config_path=path,
                                          kinds=["aggregator"])] == ["agg"]
    assert [s.name for s in build_sources(settings, config_path=path,
                                          kinds=["career_page"])] == ["page"]


def test_career_page_is_not_enabled_by_default():
    """It is the adapter that fetches arbitrary HTML, so it is opt-in."""
    from pitwall.config import Settings

    assert Settings(_env_file=None).jobs_sources_enabled == ["ats_json", "aggregator"]


def test_a_misconfigured_source_is_skipped_not_fatal(settings, tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - {name: broken, kind: aggregator}\n"
        "  - {name: fine, kind: aggregator, sitemap_url: 'https://a.test/s.xml'}\n",
        encoding="utf-8",
    )
    assert [s.name for s in build_sources(settings, config_path=path)] == ["fine"]


def test_a_missing_config_file_is_not_an_error(settings):
    assert load_source_config(Path("/definitely/not/here.yaml")) == []


# ───────────────────────────── small helpers ─────────────────────────────


def test_dates_normalise_from_every_shape_boards_use():
    assert iso_date("2026-08-24T08:47:00Z").startswith("2026-08-24")
    assert iso_date(1553186035299).startswith("2019-")       # Lever epoch millis
    assert iso_date("2026-08-21").startswith("2026-08-21")
    assert iso_date(None) is None
    assert iso_date("") is None


def test_html_descriptions_become_text():
    out = strip_html("<p>Hello<br>world</p><script>evil()</script>&amp;")
    assert "Hello" in out and "world" in out
    assert "<" not in out and "evil" not in out
    assert "&" in out
