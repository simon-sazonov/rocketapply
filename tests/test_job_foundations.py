"""Phase 0 of the job-search expansion: the plumbing every new source relies on.

* robots.txt read per RFC 9309 (wildcards, longest match), and the one narrow
  exemption — an API used under a key and its terms;
* a client that is polite per host rather than globally, so sources can run
  side by side;
* a quota ledger over rolling windows, and the pacing rule on top of it;
* Adzuna searching in a rotation that loses nothing when it is cut short;
* what a source states (remote, country, the search) surviving into the row.

No network: `httpx.get` / `httpx.post` are stubbed at the module.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pitwall.sources.jobs import JobCache, JobPosting, PoliteClient, RobotsDisallowed
from pitwall.sources.jobs import adzuna as adzuna_mod
from pitwall.sources.jobs import robots as robots_txt
from pitwall.sources.jobs.base import RobotsGate
from pitwall.sources.jobs.budget import SourceBudget, Usage, allowance

FIXTURES = Path(__file__).parent / "fixtures" / "jobs"
UA = "PitWall/2.0 (personal job-alerting; +robots-compliant)"


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def stub_http(monkeypatch, routes):
    seen: list[str] = []

    def fake(url, **kwargs):
        seen.append(url)
        for needle, response in routes.items():
            if needle in url:
                if isinstance(response, Exception):
                    raise response
                return response
        return FakeResponse("", 404)

    monkeypatch.setattr(httpx, "get", fake)
    monkeypatch.setattr(httpx, "post", fake)
    return seen


@pytest.fixture()
def client(settings, tmp_path):
    settings.jobs_crawl_delay = 0.0
    return PoliteClient(settings, cache=JobCache(root=tmp_path / "jobcache", ttl_days=0))


# ───────────────────────────── robots.txt ────────────────────────────────


HIMALAYAS = """User-Agent: *
Allow: /
Disallow: /apply
Disallow: /jobs?page=
Disallow: /jobs*&page=
Disallow: /jobs/*?page=
Disallow: /companies?page=
"""


def test_a_wildcard_rule_blocks_what_the_standard_library_let_through():
    """Himalayas' real file (28 Sep 2026) keeps crawlers off pagination with
    `Disallow: /jobs*&page=`. `urllib.robotparser` compares literally, so it let
    `?q=ml&page=2` through; paging is a refusal, and the search is not."""
    rules = robots_txt.parse(HIMALAYAS)
    assert rules.allows(UA, "https://himalayas.app/jobs/api/search?q=ml") is True
    assert rules.allows(UA, "https://himalayas.app/jobs/api/search?q=ml&country=PT") is True
    assert rules.allows(UA, "https://himalayas.app/jobs/api/search?q=ml&page=2") is False
    assert rules.allows(UA, "https://himalayas.app/jobs/api/search?page=2") is False
    assert rules.allows(UA, "https://himalayas.app/apply/123") is False


def test_the_longest_rule_wins_not_the_first():
    rules = robots_txt.parse("User-agent: *\nDisallow: /\nAllow: /jobs/\n")
    assert rules.allows(UA, "https://x.test/jobs/1") is True
    assert rules.allows(UA, "https://x.test/about") is False


def test_allow_wins_a_tie():
    rules = robots_txt.parse("User-agent: *\nDisallow: /api\nAllow: /api\n")
    assert rules.allows(UA, "https://x.test/api/v1") is True


def test_a_dollar_anchors_the_end():
    rules = robots_txt.parse("User-agent: *\nDisallow: /*.pdf$\n")
    assert rules.allows(UA, "https://x.test/cv.pdf") is False
    assert rules.allows(UA, "https://x.test/cv.pdf?v=2") is True


def test_a_group_naming_this_client_replaces_the_star_group():
    text = ("User-agent: *\nDisallow: /\n\n"
            "User-agent: PitWall\nAllow: /\nDisallow: /private\n")
    rules = robots_txt.parse(text)
    assert rules.allows(UA, "https://x.test/jobs") is True
    assert rules.allows(UA, "https://x.test/private/a") is False
    assert rules.allows("OtherBot/1.0", "https://x.test/jobs") is False


def test_consecutive_user_agent_lines_share_one_group():
    rules = robots_txt.parse("User-agent: a\nUser-agent: *\nDisallow: /x\n")
    assert rules.allows(UA, "https://x.test/x") is False


def test_an_empty_disallow_disallows_nothing_and_robots_txt_is_always_readable():
    rules = robots_txt.parse("User-agent: *\nDisallow:\n")
    assert rules.allows(UA, "https://x.test/anything") is True
    assert robots_txt.parse("User-agent: *\nDisallow: /\n").allows(
        UA, "https://x.test/robots.txt")


def test_the_real_itjobs_file_allows_the_api_and_honours_its_delay():
    text = ("User-agent: *\nCrawl-delay: 1\n\nDisallow:\nDisallow: /*?lang=\n"
            "Disallow: /admin\nAllow: /user/login\nDisallow: /user/*\n\n"
            "User-agent: GPTBot\nDisallow: /\n")
    rules = robots_txt.parse(text)
    assert rules.allows(UA, "https://api.itjobs.pt/job/search.json?q=data") is True
    assert rules.allows(UA, "https://www.itjobs.pt/emprego?lang=en") is False
    assert rules.crawl_delay(UA) == 1.0


# ─────────────────── the keyed-API exemption, and only that ──────────────


def test_a_terms_governed_request_skips_robots_and_nothing_else_does(client, monkeypatch):
    seen = stub_http(monkeypatch, {
        "robots.txt": FakeResponse("User-agent: *\nDisallow: /\n"),
        "api.test/v1": FakeResponse('{"ok": true}'),
    })
    client.robots.reset()
    with pytest.raises(RobotsDisallowed):
        client.get_json("https://api.test/v1/search")
    assert client.get_json("https://api.test/v1/search", terms_govern=True) == {"ok": True}
    assert sum(1 for u in seen if u.endswith("/v1/search")) == 1


def test_adzuna_declares_itself_terms_governed(settings):
    source = adzuna_mod.AdzunaSource("id", "key", queries=["a"], countries=["DE"],
                                     settings=settings, client=object())
    assert source.terms_govern is True


# ─────────────────────── a client polite per host ────────────────────────


def test_two_hosts_do_not_wait_for_each_other(settings, tmp_path, monkeypatch):
    """The old client slept while holding one lock: every host waited out every
    other host's delay. Now only requests to the SAME host are spaced."""
    settings.jobs_crawl_delay = 0.3
    client = PoliteClient(settings, cache=JobCache(root=tmp_path / "c", ttl_days=0),
                          robots=RobotsGate(UA))
    client.robots._cache.update({"https://a.test": None, "https://b.test": None})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: FakeResponse("{}"))

    def hit(host, n):
        for i in range(n):
            client.get(f"https://{host}/{i}", no_cache=True)

    started = time.monotonic()
    threads = [threading.Thread(target=hit, args=(h, 3)) for h in ("a.test", "b.test")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    # Three requests per host = two gaps of 0.3 s. In parallel: ~0.6 s, not ~1.5 s.
    assert elapsed < 1.1, elapsed
    assert client.network_requests == {"a.test": 3, "b.test": 3}


def test_a_published_rate_limit_becomes_a_floor_on_the_gap(client, monkeypatch):
    client.robots._cache["https://api.test"] = None
    client.set_min_delay("https://api.test", 0.25)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: FakeResponse("{}"))
    started = time.monotonic()
    for i in range(3):
        client.get(f"https://api.test/{i}", no_cache=True, terms_govern=True)
    assert time.monotonic() - started >= 0.5


def test_a_cache_hit_is_not_a_network_request(client, monkeypatch):
    client.robots._cache["https://x.test"] = None
    client.cache.ttl_days = 7
    monkeypatch.setattr(httpx, "get", lambda url, **kw: FakeResponse("{}"))
    client.get("https://x.test/a")
    assert client.last_was_network() is True
    client.get("https://x.test/a")
    assert client.last_was_network() is False


def test_a_post_search_is_cached_on_its_body(client, monkeypatch):
    calls = []

    def post(url, **kw):
        calls.append(kw["json"])
        return FakeResponse(json.dumps({"echo": kw["json"]}))

    client.robots._cache["https://s.test"] = None
    client.cache.ttl_days = 7
    monkeypatch.setattr(httpx, "post", post)
    assert client.post_json("https://s.test/q", {"q": "ml"}) == {"echo": {"q": "ml"}}
    client.post_json("https://s.test/q", {"q": "ml"})
    client.post_json("https://s.test/q", {"q": "data"})
    assert calls == [{"q": "ml"}, {"q": "data"}]


def test_a_429_is_its_own_error(client, monkeypatch):
    from pitwall.sources.jobs import RateLimited

    client.robots._cache["https://x.test"] = None
    monkeypatch.setattr(httpx, "get", lambda url, **kw: FakeResponse("", 429))
    with pytest.raises(RateLimited):
        client.get("https://x.test/a")


# ───────────────────────────── the ledger ────────────────────────────────


ADZUNA = SourceBudget(per_day=250, per_week=1000, per_month=2500)


def test_the_tightest_window_decides():
    assert ADZUNA.remaining(Usage(day=0, week=0, month=0)) == 250
    assert ADZUNA.remaining(Usage(day=100, week=950, month=950)) == 50
    assert ADZUNA.remaining(Usage(day=0, week=900, month=2480)) == 20
    assert ADZUNA.remaining(Usage(day=300, week=0, month=0)) == 0


def test_the_steady_rate_is_what_the_month_can_sustain():
    assert ADZUNA.steady_per_day() == 80


def test_auto_bursts_during_a_backfill_and_evens_out_after():
    fresh = Usage(day=0, week=0, month=0)
    assert allowance(ADZUNA, fresh, pace="auto", backfill_pending=True) == 250
    assert allowance(ADZUNA, fresh, pace="auto", backfill_pending=False) == 80
    assert allowance(ADZUNA, Usage(day=50, week=50, month=50), pace="even") == 30
    assert allowance(ADZUNA, Usage(day=0, week=990, month=990), pace="burst") == 10


def test_used_since_counts_a_rolling_window(repos):
    from pitwall.db import today_utc

    today = datetime.strptime(today_utc(), "%Y-%m-%d")
    for days_ago, n in ((0, 5), (3, 7), (6, 11), (7, 13), (30, 17), (31, 19)):
        day = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        repos.conn.execute("INSERT INTO quota (provider, day, requests) VALUES ('adzuna', ?, ?)",
                           (day, n))
    assert repos.quota.used_since("adzuna", 1) == 5
    assert repos.quota.used_since("adzuna", 7) == 5 + 7 + 11
    assert repos.quota.used_since("adzuna", 31) == 5 + 7 + 11 + 13 + 17


# ───────────────────────────── Adzuna's rotation ─────────────────────────


def _page(n, total, start=0):
    return {"count": total, "results": [
        {"id": str(start + i), "title": f"Data Scientist {start + i}",
         "redirect_url": f"https://adzuna.test/{start + i}", "company": {"display_name": "X"},
         "location": {"display_name": "Berlin"}, "description": "…"}
        for i in range(n)
    ]}


class ScriptedClient:
    """Answers Adzuna URLs from a dict keyed on (country, page); counts calls."""

    def __init__(self, pages):
        self.pages = pages
        self.urls: list[str] = []

    def last_was_network(self):
        return True

    def get_json(self, url, *, no_cache=False, cache_key=None, terms_govern=False):
        assert terms_govern is True, "Adzuna must use its terms exemption"
        self.urls.append(cache_key)
        country = url.split("/jobs/")[1].split("/")[0]
        page = int(url.split("/search/")[1].split("?")[0])
        return self.pages.get((country, page), {"count": 0, "results": []})


def test_a_backfill_reads_every_page_back_the_full_window(settings):
    client = ScriptedClient({("de", 1): _page(50, 120), ("de", 2): _page(50, 120, 50),
                             ("de", 3): _page(20, 120, 100)})
    source = adzuna_mod.AdzunaSource("id", "key", queries=["data scientist"], countries=["DE"],
                                     max_requests=None, settings=settings, client=client)
    postings = source.fetch()
    assert len(postings) == 120 and source.requests_made == 3
    assert all("max_days_old=30" in u for u in client.urls)
    [visit] = source.visits
    assert visit.complete and visit.total == 120 and visit.requests == 3
    assert postings[0].found_query == "data scientist" and postings[0].country_hint == "DE"


def test_after_a_backfill_a_visit_asks_only_for_what_is_new(settings):
    now = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    cursors = {("data scientist", "de"): {"backfilled": 1,
                                          "last_fetched_at": "2026-09-28 07:00:00"}}
    client = ScriptedClient({("de", 1): _page(3, 3)})
    source = adzuna_mod.AdzunaSource("id", "key", queries=["data scientist"], countries=["DE"],
                                     cursors=cursors, now=now, settings=settings, client=client)
    assert source.backfill_pending() is False
    source.fetch()
    assert "max_days_old=4" in client.urls[0], "three days since the last visit, plus one"


def test_the_allowance_stops_mid_search_without_moving_its_cursor(settings):
    client = ScriptedClient({("de", 1): _page(50, 500), ("de", 2): _page(50, 500, 50)})
    source = adzuna_mod.AdzunaSource("id", "key", queries=["data scientist"],
                                     countries=["DE", "GB"], max_requests=2,
                                     settings=settings, client=client)
    source.fetch()
    assert source.requests_made == 2
    [visit] = source.visits
    assert visit.complete is False, "an unfinished read is redone, never skipped"


def test_never_read_searches_come_first_then_the_longest_unvisited(settings):
    cursors = {
        ("a", "de"): {"backfilled": 1, "last_fetched_at": "2026-09-27 07:00:00"},
        ("a", "gb"): {"backfilled": 1, "last_fetched_at": "2026-09-25 07:00:00"},
    }
    source = adzuna_mod.AdzunaSource("id", "key", queries=["a", "b"], countries=["DE", "GB"],
                                     cursors=cursors, max_requests=None, settings=settings,
                                     client=object())
    assert source.plan() == [("b", "de"), ("b", "gb"), ("a", "gb"), ("a", "de")]


def test_a_429_stops_the_run_and_the_request_still_counts(settings):
    from pitwall.sources.jobs import RateLimited

    class Limited(ScriptedClient):
        def get_json(self, url, **kw):
            super().get_json(url, **kw)
            raise RateLimited("adzuna: rate limited (429)")

    client = Limited({})
    source = adzuna_mod.AdzunaSource("id", "key", queries=["a", "b"], countries=["DE"],
                                     settings=settings, client=client)
    assert source.fetch() == []
    assert source.requests_made == 1 and len(client.urls) == 1


# ─────────────────── spend recorded on the owning thread ─────────────────


def test_a_run_records_adzunas_spend_and_cursors(settings, repos):
    from pitwall.jobsearch.run import JobSearch
    from pitwall.pipeline import Pipeline

    client = ScriptedClient({("de", 1): _page(2, 2)})
    source = adzuna_mod.AdzunaSource("id", "key", queries=["data scientist"], countries=["DE"],
                                     settings=settings, client=client)
    source.fetch()
    search = JobSearch.__new__(JobSearch)
    search.pipe = Pipeline(settings, repos)
    search.repos = repos
    search.record_spend([source])
    assert repos.quota.used_since("adzuna", 1) == 1
    row = repos.source_queries.for_source("adzuna")[("data scientist", "de")]
    assert row["backfilled"] == 1 and row["found"] == 2 and row["last_fetched_at"]


# ─────────────────── source facts reach the stored row ───────────────────


def test_what_a_source_states_is_stored(repos):
    source_id = repos.job_sources.upsert("adzuna", "aggregator_api")
    posting = JobPosting(title="ML Engineer", url="https://adzuna.test/1", remote=True,
                         country_hint="DE", found_query="machine learning")
    job_id = repos.jobs.upsert(source_id, posting).job_id
    row = repos.conn.execute("SELECT source_remote, source_country, found_query FROM jobs"
                             " WHERE id = ?", (job_id,)).fetchone()
    assert tuple(row) == (1, "DE", "machine learning")


def test_the_schema_has_the_source_facts_and_source_queries(repos):
    from pitwall.db import SCHEMA_VERSION

    assert SCHEMA_VERSION == 10
    cols = {r["name"] for r in repos.conn.execute("PRAGMA table_info(jobs)")}
    assert {"source_remote", "source_country", "found_query", "remote_regions"} <= cols
    assert repos.conn.execute("SELECT COUNT(*) FROM source_queries").fetchone()[0] == 0


# ───────────────────── remote is a list, and silence is unknown ──────────


def _cfg(**work):
    from pitwall.jobsearch.settings import JobSearchConfig

    return JobSearchConfig.model_validate({
        "locations": {"countries": ["DE", "PT", "US", "GB"], "home_country": "PT"},
        "work_authorization": {"needs_sponsorship": True, "authorized_countries": ["PT"],
                               **work},
    })


def test_a_remote_job_naming_several_countries_keeps_them_all():
    from pitwall.jobsearch.locations import parse_location

    loc = parse_location("Remote - United States, Canada")
    assert loc.remote_status == "remote" and set(loc.remote_regions) == {"US", "CA"}
    loc = parse_location("Remote (EU) or United Kingdom")
    assert loc.remote_regions == ("EU", "GB")
    assert parse_location("Remote, Worldwide").remote_regions == ("WORLD",)


def test_bare_remote_is_unknown_not_open_everywhere():
    from pitwall.jobsearch.filters import remote_open_to_you

    cfg = _cfg()
    assert remote_open_to_you(None, cfg) is None
    assert remote_open_to_you([], cfg) is None
    assert remote_open_to_you(["US", "CA"], cfg) is False
    assert remote_open_to_you(["US", "PT"], cfg) is True
    assert remote_open_to_you(["EU"], cfg) is True, "Portugal is in the EU"
    assert remote_open_to_you(["WORLD"], cfg) is True


def test_a_us_only_remote_job_is_filtered_for_someone_who_can_work_only_in_portugal():
    from pitwall.jobsearch.filters import hard_filter

    job = {"remote_status": "remote", "remote_regions": '["US", "CA"]', "cities": "[]",
           "countries": '["US", "CA"]', "role_labels": '["MACHINE_LEARNING"]',
           "title": "ML Engineer"}
    codes = [r.code for r in hard_filter(job, _cfg())]
    assert codes == ["remote"]


def test_bare_remote_no_longer_claims_no_sponsorship_is_needed():
    from pitwall.jobsearch.scoring import location_component, visa_component

    job = {"remote_status": "remote", "remote_regions": "[]", "cities": "[]",
           "countries": "[]", "visa_status": "UNKNOWN"}
    plus, minus = [], []
    comp, fit = visa_component(job, _cfg(), plus, minus)
    assert fit == "unknown" and not any("No sponsorship needed" in p for p in plus)
    loc = location_component(job, _cfg(), [], minus)
    assert loc.label == "unknown" and any("region not stated" in m for m in minus)


def test_adzunas_search_country_places_a_bare_remote_job():
    """The payload says only "Remote"; the search was Adzuna Germany."""
    from pitwall.jobsearch.normalize import normalize
    from pitwall.jobsearch.skills import vocabulary

    row = {"title": "Data Scientist", "location": "Remote", "description": "Python.",
           "source_country": "DE", "source_remote": 1, "raw_json": "{}"}
    fields = normalize(row, vocab=vocabulary())
    assert fields["remote_status"] == "remote"
    assert json.loads(fields["remote_regions"]) == ["DE"]


def test_eu_and_na_are_regions_even_though_they_have_two_letters():
    """Seven stored "Remote (EU)" jobs were filtered as remote-within-a-country
    called EU. Found while writing these tests."""
    from pitwall.jobsearch.filters import hard_filter

    job = {"remote_status": "remote", "remote_region": "EU", "cities": "[]",
           "countries": "[]", "role_labels": '["MACHINE_LEARNING"]', "title": "ML Engineer"}
    assert hard_filter(job, _cfg()) == []


def test_an_eu_work_permit_requirement_is_met_in_the_country_you_can_work_in():
    from pitwall.jobsearch.filters import hard_filter

    base = {"remote_status": "onsite", "cities": "[]", "role_labels": '["MACHINE_LEARNING"]',
            "title": "ML Engineer", "visa_status": "NO_SPONSORSHIP", "visa_scope": "EU",
            "visa_evidence": "must hold an EU work permit"}
    lisbon = {**base, "countries": '["PT"]'}
    berlin = {**base, "countries": '["DE"]'}
    assert hard_filter(lisbon, _cfg()) == []
    assert [r.code for r in hard_filter(berlin, _cfg())] == ["visa"]


def test_a_title_search_the_board_got_wrong_is_filtered_and_abandoned(settings):
    """Adzuna Italy drops "ai" (an Italian preposition): "ai engineer" came back
    as every engineer in Italy, 4% of 990 titles on the first live run."""
    def engineers(n, start=0):
        return {"count": 3000, "results": [
            {"id": str(start + i), "title": ("AI Engineer" if i == 0 else f"Field Engineer {i}"),
             "redirect_url": f"https://adzuna.test/{start + i}", "company": {"display_name": "X"},
             "location": {"display_name": "Milano"}, "description": "…"} for i in range(n)]}

    client = ScriptedClient({("it", 1): engineers(50), ("it", 2): engineers(50, 50)})
    source = adzuna_mod.AdzunaSource("id", "key", queries=["ai engineer"], countries=["IT"],
                                     max_requests=None, settings=settings, client=client)
    postings = source.fetch()
    assert [p.title for p in postings] == ["AI Engineer"]
    assert source.requests_made == 1 and source.visits[0].complete


# ───────────────────── Phase 3: languages and US levels ──────────────────


@pytest.mark.parametrize("title, level", [
    ("Machine Learning Engineer I", "entry"),
    ("Machine Learning Engineer II", "unknown"),
    ("Data Scientist III", "mid"),
    ("Data Scientist IV", "senior"),
    ("Machine Learning Engineer 5 (IC)", "senior"),
    ("Data Scientist (5649)", "unknown"),
    ("Python 3 Developer", "unknown"),
])
def test_us_ladder_suffixes_read_as_a_level(title, level):
    from pitwall.jobsearch.experience import title_level

    assert title_level(title) == level


@pytest.mark.parametrize("lang, text", [
    ("pt", "Estamos à procura de um cientista de dados para a nossa equipa. Você vai trabalhar "
           "com os dados e também com os modelos, não é necessário ter experiência."),
    ("sv", "Vi söker en data scientist till vårt team. Du kommer att arbeta med maskininlärning "
           "och det är viktigt att du har erfarenhet av Python och som vill utvecklas inom AI."),
    ("es", "Buscamos un científico de datos para nuestro equipo. Trabajarás con los datos y las "
           "herramientas del equipo, y también con los modelos más avanzados para sus clientes."),
    ("it", "Siamo alla ricerca di un data scientist per il nostro team. Lavorerai con gli "
           "strumenti della piattaforma e delle analisi che sono il cuore del prodotto per noi."),
])
def test_the_languages_of_the_new_countries_are_recognised(lang, text):
    from pitwall.jobsearch.normalize import languages_required, posting_language

    assert posting_language(text) == lang
    assert lang in languages_required("Data Scientist", text, lang)


# ─────────────────── Phase 3: a wish is not a requirement ─────────────────


POSTING = """What you'll bring
• Python and SQL
• Experience with PyTorch
Nice to have
• Kubernetes
• Spark
What we offer
• 30 days holiday
Experience with AWS is a plus. You know Docker."""


def test_wishes_are_told_apart_from_requirements():
    from pitwall.jobsearch.normalize import normalize
    from pitwall.jobsearch.skills import vocabulary

    fields = normalize({"title": "Data Scientist", "description": POSTING, "raw_json": "{}"},
                       vocab=vocabulary())
    nice = set(json.loads(fields["skills_nice"]))
    assert {"kubernetes", "aws"} <= nice
    assert not nice & {"python", "sql", "pytorch", "docker"}


def test_a_missing_wish_costs_a_third_of_a_missing_requirement():
    from pitwall.jobsearch.scoring import skills_component
    from pitwall.jobsearch.skills import OwnedSkill, vocabulary

    vocab = vocabulary()
    owned = {s: OwnedSkill(s, proficiency=3) for s in ("python", "sql", "pytorch")}
    wanted = '["python", "sql", "pytorch", "kubernetes", "spark", "aws"]'
    as_wishes = {"skills": wanted, "skills_nice": '["kubernetes", "spark", "aws"]'}
    as_requirements = {"skills": wanted, "skills_nice": "[]"}
    minus: list[str] = []
    wish, *_ = skills_component(as_wishes, owned, vocab, [], minus)
    req, *_ = skills_component(as_requirements, owned, vocab, [], [])
    assert (req.label, wish.label) == ("ok", "strong")
    assert wish.value > req.value + 0.25
    assert any(m.startswith("? Nice to have") for m in minus)
    assert not any(m.startswith("- Not in your profile") for m in minus)


# ─────────────────── Phase 3: sponsor registers, agencies ─────────────────


UK_CSV = """Organisation Name,Town/City,County,Type & Rating,Route
 Google UK Limited,London,,Worker (A rating),Skilled Worker
 Wayve Technologies Ltd,London,,Worker (A rating),Skilled Worker
 A Care Home Ltd,Leeds,,Worker (A rating),Health and Care
"""
NL_HTML = """<table><tr><th>Organisation</th><th>KVK number</th></tr>
<tr><td>Adyen N.V.</td><td>34259528</td></tr>
<tr><td>""AAE"" Advanced Automated Equipment B.V.</td><td>17037842</td></tr></table>"""
US_CSV = """Fiscal Year,Employer (Petitioner) Name,Tax ID,Industry (NAICS) Code,Petitioner City,\
Petitioner State,Petitioner Zip Code,Initial Approval,Initial Denial,Continuing Approval,\
Continuing Denial
2025,WAYMO LLC,1234,54 - Professional,MOUNTAIN VIEW,CA,94043,120,2,300,1
2025,TINY SHOP INC,5678,44 - Retail,AUSTIN,TX,73301,0,1,0,0
"""


def _index():
    from pitwall.jobsearch.sponsors import SponsorIndex, parse_nl, parse_uk, parse_us

    return SponsorIndex([parse_uk(UK_CSV), parse_nl(NL_HTML), parse_us(US_CSV)])


def test_registers_are_read_and_matched_by_name_in_the_jobs_country():
    index = _index()
    assert "Skilled Worker" in index.lookup("Google", ["GB"]), "UK Limited and country words go"
    assert index.lookup("Wayve", ["GB"])
    assert index.lookup("A Care Home", ["GB"]) is None, "not a Skilled Worker route"
    assert index.lookup("Adyen", ["NL"]) and index.lookup("Adyen", ["GB"]) is None
    assert "420 H-1B approval(s) in FY2025" in index.lookup("Waymo", ["US"])
    assert index.lookup("Tiny Shop", ["US"]) is None, "no approvals, no signal"


def test_a_register_lifts_an_unknown_visa_but_never_overrules_the_posting():
    from pitwall.jobsearch.scoring import visa_component

    cfg = _cfg()
    silent = {"visa_status": "UNKNOWN", "countries": '["GB"]', "cities": '["London"]',
              "posting_company": "Wayve"}
    plus: list[str] = []
    comp, fit = visa_component(silent, cfg, plus, [], _index())
    assert fit == "likely" and comp.value == 0.8 and "Licensed visa sponsor" in plus[0]
    refuses = {**silent, "visa_status": "NO_SPONSORSHIP", "visa_evidence": "no sponsorship"}
    assert visa_component(refuses, cfg, [], [], _index())[1] == "no"
    agency = {**silent, "agency": 1}
    assert visa_component(agency, cfg, [], [], _index())[1] == "unknown"


def test_the_registers_load_from_disk_including_a_downloaded_us_file(settings):
    from pitwall.jobsearch import sponsors

    folder = sponsors.sponsors_dir(settings)
    folder.mkdir(parents=True)
    (folder / sponsors.UK_FILE).write_text(UK_CSV, encoding="utf-8")
    (folder / "Employer Information.csv").write_text(US_CSV, encoding="utf-8")
    index = sponsors.load(settings)
    assert set(index.registers) == {"GB", "US"}


def test_the_refresh_reads_todays_link_and_keeps_the_old_copy_on_failure(settings):
    from pitwall.jobsearch import sponsors
    from pitwall.sources.jobs import SourceError

    class FakeClient:
        def __init__(self):
            self.urls = []

        def get(self, url, **kw):
            self.urls.append(url)
            if url == sponsors.UK_PAGE:
                return '<a href="https://assets.publishing.service.gov.uk/media/x/Reg.csv">'
            if url.endswith("Reg.csv"):
                return UK_CSV
            raise SourceError("ind.nl: HTTP 503")

    client = FakeClient()
    notes = sponsors.refresh(settings, client)
    folder = sponsors.sponsors_dir(settings)
    assert (folder / sponsors.UK_FILE).read_text(encoding="utf-8") == UK_CSV
    assert any("NL register" in n for n in notes)
    client.urls.clear()
    sponsors.refresh(settings, client)
    assert sponsors.UK_PAGE not in client.urls, "a fresh copy is not downloaded again"
    assert sponsors.NL_PAGE in client.urls, "a failed one is retried next run"


@pytest.mark.parametrize("company, text, agency", [
    ("RANDSTAD PTE. LIMITED", "", True),
    ("Brunel GmbH NL Mannheim", "", True),
    ("Acme", "We are recruiting on behalf of our client, a fintech in Lisbon.", True),
    ("Acme", "Für unseren Kunden suchen wir einen Data Scientist.", True),
    ("Waymo", "Join our team.", False),
])
def test_recruitment_agencies_are_marked(company, text, agency):
    from pitwall.jobsearch.normalize import is_agency

    assert is_agency(company, text) is agency


@pytest.mark.parametrize("title, label", [
    ("Ai Integration Engineer", "AI"),
    ("Principal Ai Engineer - Context", "AI"),
    ("Machine Learning Co-Op (Summer 2027)", "MACHINE_LEARNING"),
    ("Stage : STAGE - IA Machine Learning appliqué à la simulation physique F/H",
     "MACHINE_LEARNING"),
    ("Early-stage startup AI Deployment Strategist", "OTHER"),
    ("Hai Engineer", "OTHER"),
])
def test_titles_the_first_live_runs_misread(title, label):
    """Sampled from the OTHER pile after the Phase 1 run (28 Sep 2026)."""
    from pitwall.jobsearch.roles import classify

    assert label in classify(title).labels


# ───────────────────── Phase 3: how much a posting reads like the CV ─────


def test_cv_similarity_ranks_the_posting_that_shares_the_cvs_rare_words_first(tmp_path):
    from pitwall.jobsearch.similarity import CVSimilarity, profile_text

    (tmp_path / "config").mkdir()
    (tmp_path / "profile").mkdir()
    (tmp_path / "config" / "my_profile.md").write_text(
        "MSc thesis on Monte Carlo race strategy simulation and tyre degradation forecasting "
        "with Bayesian time-series models.", encoding="utf-8")
    (tmp_path / "profile" / "projects.yaml").write_text(
        "- name: Pit stop optimiser\n  summary: stochastic optimisation of pit windows\n",
        encoding="utf-8")
    (tmp_path / "profile" / "personal.yaml").write_text("phone: '+351 000'\n", encoding="utf-8")
    cv = profile_text(tmp_path / "config", tmp_path / "profile")
    assert "Monte Carlo" in cv and "stochastic" in cv and "+351" not in cv

    postings = [
        "Strategy engineer: Monte Carlo simulation of race strategy and tyre degradation.",
        "Frontend developer: React, CSS and accessibility for our marketing website.",
        "Data scientist: forecasting demand with time-series models.",
    ]
    model = CVSimilarity(cv, postings)
    ranks = model.rank([model.cosine(p) for p in postings])
    assert ranks[0] > ranks[2] > ranks[1]


def test_the_cv_match_is_a_component_and_unknown_without_a_cv():
    from pitwall.jobsearch.scoring import fit_component

    assert fit_component({}, [], []).label == "unknown"
    plus: list[str] = []
    assert fit_component({"cv_similarity": 0.95}, plus, []).label == "strong"
    assert "Reads like your CV" in plus[0]


# ─────────────────────── Phase 3: calibration on ratings ──────────────────


def test_auc_is_the_chance_a_good_job_outscores_a_bad_one():
    from pitwall.jobsearch.calibrate import auc

    assert auc([0.9, 0.8, 0.2, 0.1], [True, True, False, False]) == 1.0
    assert auc([0.1, 0.2, 0.8, 0.9], [True, True, False, False]) == 0.0
    assert auc([0.5, 0.5], [True, False]) == 0.5
    assert auc([0.5], [True]) is None


def test_calibration_finds_the_part_that_agrees_with_the_owner(repos):
    from pitwall.jobsearch.calibrate import MIN_EACH, calibrate
    from pitwall.sources.jobs import JobPosting

    src = repos.job_sources.upsert("t", "job_board")
    for i in range(2 * MIN_EACH + 4):
        good = i % 2 == 0
        job = repos.jobs.upsert(src, JobPosting(title=f"J{i}", url=f"https://t.test/{i}")).job_id
        comps = {"role": {"value": 0.9 if good else 0.2},       # agrees with the owner
                 "company": {"value": 0.5}}                    # says nothing
        repos.conn.execute("INSERT INTO job_matches (job_id, config_hash, passed, score, label,"
                           " components) VALUES (?, 'x', 1, 60, 'good', ?)",
                           (job, json.dumps(comps)))
        repos.conn.execute("INSERT INTO job_review (cluster_id, feedback) VALUES (?, ?)",
                           (job, "good_match" if good else "bad_match"))
    report = calibrate(repos.conn, {"role": 50.0, "company": 50.0})
    assert report.enough and report.auc["role"] == 1.0 and report.auc["company"] == 0.5
    assert report.proposed["role"] > report.proposed["company"]
    assert sum(report.proposed.values()) == pytest.approx(100.0, abs=0.2)


def test_dismissing_as_a_duplicate_says_nothing_about_fit(repos):
    from pitwall.jobsearch.calibrate import examples
    from pitwall.sources.jobs import JobPosting

    src = repos.job_sources.upsert("t", "job_board")
    for i, reason in enumerate(("Duplicate", "Too senior")):
        job = repos.jobs.upsert(src, JobPosting(title=f"J{i}", url=f"https://t.test/{i}")).job_id
        repos.conn.execute("INSERT INTO job_matches (job_id, config_hash, passed, score, label,"
                           " components) VALUES (?, 'x', 1, 60, 'good', '{}')", (job,))
        repos.conn.execute("INSERT INTO job_review (cluster_id, dismissed_at, dismissed_reason)"
                           " VALUES (?, datetime('now'), ?)", (job, reason))
    [only] = examples(repos.conn)
    assert only.good is False


def test_only_a_sent_application_counts_and_an_explicit_rating_wins(repos):
    from pitwall.jobsearch.calibrate import examples
    from pitwall.sources.jobs import JobPosting

    src = repos.job_sources.upsert("t", "job_board")
    cases = {  # status, explicit feedback -> expected label (None = not an example)
        "cv_ready_no_rating": ("CV_READY", None, None),
        "shortlisted": ("SHORTLISTED", None, None),
        "in_process": ("IN_PROCESS", None, None),
        "applied": ("APPLIED", None, True),
        "rejected": ("REJECTED", None, True),
        "cv_ready_thumbs_down": ("CV_READY", "bad_match", False),
        "applied_thumbs_down": ("APPLIED", "bad_match", False),
        "shortlisted_thumbs_up": ("SHORTLISTED", "good_match", True),
    }
    ids = {}
    for i, (name, (status, feedback, _)) in enumerate(cases.items()):
        job = repos.jobs.upsert(src, JobPosting(title=name, url=f"https://t.test/{i}")).job_id
        ids[job] = name
        repos.conn.execute("INSERT INTO job_matches (job_id, config_hash, passed, score, label,"
                           " components) VALUES (?, 'x', 1, 60, 'good', '{}')", (job,))
        repos.conn.execute("INSERT INTO applications (cluster_id, job_id, company_name, title,"
                           " status) VALUES (?, ?, 'Co', ?, ?)", (job, job, name, status))
        if feedback:
            repos.conn.execute("INSERT INTO job_review (cluster_id, feedback) VALUES (?, ?)",
                               (job, feedback))
    got = {ids[e.cluster_id]: e.good for e in examples(repos.conn)}
    assert got == {n: want for n, (_, _, want) in cases.items() if want is not None}


# ─────────────────────── Phase 4: searches that pay ──────────────────────


def test_a_productive_search_comes_round_sooner_than_a_barren_one(settings):
    now = datetime(2026, 10, 10, 7, tzinfo=UTC)
    cursors = {
        # visited 2 days ago; 1 relevant job per request
        ("data scientist", "de"): {"backfilled": 1, "last_fetched_at": "2026-10-08 07:00:00",
                                   "requests": 10, "relevant": 10},
        # visited 5 days ago; never found anything relevant
        ("race strategy engineer", "de"): {"backfilled": 1, "requests": 10, "relevant": 0,
                                           "last_fetched_at": "2026-10-05 07:00:00"},
    }
    source = adzuna_mod.AdzunaSource("id", "key", queries=["race strategy engineer",
                                                           "data scientist"],
                                     countries=["DE"], cursors=cursors, now=now,
                                     max_requests=None, settings=settings, client=object())
    assert source.plan()[0] == ("data scientist", "de")
    # …but a barren search is only deferred: after long enough it comes first.
    cursors[("race strategy engineer", "de")]["last_fetched_at"] = "2026-09-20 07:00:00"
    assert source.plan()[0] == ("race strategy engineer", "de")


def test_yield_is_counted_from_what_the_filters_passed(repos):
    from pitwall.jobsearch.market import cursors_with_yield

    src = repos.job_sources.upsert("adzuna", "aggregator_api")
    for i, passed in enumerate((1, 1, 0)):
        job = repos.jobs.upsert(src, JobPosting(title="DS", url=f"https://a.test/{i}",
                                                country_hint="DE",
                                                found_query="data scientist")).job_id
        repos.conn.execute("INSERT INTO job_matches (job_id, config_hash, passed, score, label)"
                           " VALUES (?, 'x', ?, 60, 'good')", (job, passed))
    repos.source_queries.record("adzuna", "data scientist", "de", requests=2, found=3,
                                complete=True)
    rows = cursors_with_yield(repos, "adzuna")
    assert rows[("data scientist", "de")]["relevant"] == 2


def test_an_ai_fit_request_stays_inside_groqs_per_request_limit():
    """Eight 3,000-character postings plus the CV came back 413 on the first
    live run (28 Sep 2026): Groq's free tier refuses one request over 8k tokens."""
    from pitwall.jobsearch.run import AI_FIT_DESCRIPTION_CHARS, fit_batch_size

    cv = "x" * 6000
    n = fit_batch_size(cv, 8)
    estimate = (len(cv) + n * (AI_FIT_DESCRIPTION_CHARS + 300)) / 4 + 800 + n * 150
    assert 1 <= n < 8 and estimate <= 5000
    assert fit_batch_size("x" * 40000, 8) == 1, "never zero, even beside a huge CV"


def test_a_bad_rating_keeps_its_reason_until_the_rating_changes(repos):
    from pitwall.jobsearch import store

    store.set_feedback(repos.conn, 7, "bad_match")
    store.set_feedback_reason(repos.conn, 7, "Too senior", "  asks for 5 years  ")
    row = repos.conn.execute("SELECT feedback, feedback_reason, feedback_note FROM job_review"
                             " WHERE cluster_id = 7").fetchone()
    assert tuple(row) == ("bad_match", "Too senior", "asks for 5 years")
    store.set_feedback(repos.conn, 7, "good_match")
    row = repos.conn.execute("SELECT feedback_reason FROM job_review WHERE cluster_id = 7")
    assert row.fetchone()[0] is None


def test_the_uscis_download_is_read_as_utf16_tabs(tmp_path):
    """The real Data Hub export (28 Sep 2026): UTF-16 with a BOM, tab-separated,
    a "Line by line" first column, and "New Employment Approval" style columns."""
    from pitwall.jobsearch.sponsors import parse_us, read_text

    rows = ["Line by line\tFiscal Year   \tEmployer (Petitioner) Name\tTax ID\t"
            "New Employment Approval\tNew Employment Denial\tContinuation Approval",
            "1\t2026\tANTHROPIC PBC\t0000\t40\t1\t12",
            "2\t2026\tOPENAI OPCO LLC\t0000\t55\t0\t30"]
    path = tmp_path / "Employer Information.csv"
    path.write_bytes("\n".join(rows).encode("utf-16"))
    register = parse_us(read_text(path))
    assert register.find("Anthropic") == "52 H-1B approval(s) in FY2026"
    assert register.find("OpenAI")


# ─────────────── from Simon's first 50 ratings (28 Sep 2026) ───────────────


@pytest.mark.parametrize("title, phd", [
    ("AI Research Intern (PhD) – 3D Computer Vision", True),
    ("PhD Student Researcher, Robotics", True),
    ("2027 Summer Intern, MS/PhD, Machine Learning", False),
    ("Master or PhD Intern - Data Science", False),
])
def test_a_phd_in_the_title_is_a_requirement_unless_a_masters_is_accepted(title, phd):
    from pitwall.jobsearch.normalize import normalize
    from pitwall.jobsearch.skills import vocabulary

    fields = normalize({"title": title, "description": "Python.", "raw_json": "{}"},
                       vocab=vocabulary())
    assert (fields["education_required"] == "phd") is phd


def test_read_and_write_french_is_a_language_requirement():
    from pitwall.jobsearch.normalize import languages_required

    text = "Requirements: Read and write French: our sources and deliverables are in French."
    assert languages_required("AI Engineer Intern", text, "en") == ["fr"]
    assert languages_required("ML Engineer", "Our office sits near French bakeries.", "en") == []


# ─────────────── from Claude's 120 ratings (28 Sep 2026) ───────────────


@pytest.mark.parametrize("text, scope", [
    ("Requirement: Singaporean ONLY , due to G50 security clearance requirements.", "SG"),
    ("Job Requirements: • Singapore Citizens only. • Bachelor's degree", "SG"),
    ("Clearance Required: TS/SCI Location: Centennial, CO", "US"),
    ("candidates must be a U.S. citizen or national, U.S. permanent resident", "US"),
    ("Data Engineer - Active SC and NPPV3", "GB"),
    ("Data Engineer SC Cleared", "GB"),
])
def test_citizenship_and_clearance_requirements_are_refusals_with_a_country(text, scope):
    from pitwall.jobsearch import visa

    found = visa.extract(text)
    assert (found.status, found.scope) == ("NO_SPONSORSHIP", scope)


def test_a_citizenship_line_that_says_no_is_not_a_requirement():
    from pitwall.jobsearch import visa

    assert visa.extract("Experience: 0 years US Citizenship Required: No Python.").status \
        == "UNKNOWN"


def test_a_singapore_only_job_is_filtered_for_someone_who_can_work_only_in_portugal():
    from pitwall.jobsearch.filters import hard_filter
    from pitwall.jobsearch.normalize import normalize
    from pitwall.jobsearch.skills import vocabulary

    fields = normalize({"title": "Data Scientist", "location": "Singapore",
                        "description": "Requirement: Singaporean ONLY. Python, SQL.",
                        "raw_json": "{}"}, vocab=vocabulary())
    job = {**fields, "title": "Data Scientist"}
    from pitwall.jobsearch.settings import JobSearchConfig

    cfg = JobSearchConfig.model_validate({
        "locations": {"countries": ["SG", "PT"], "home_country": "PT"},
        "work_authorization": {"needs_sponsorship": True, "authorized_countries": ["PT"]}})
    assert [r.code for r in hard_filter(job, cfg)] == ["visa"]


def test_a_degree_opening_a_requirement_line_is_required():
    from pitwall.jobsearch.normalize import education

    text = "Requirements:\n• PhD in Computer Science, AI, or a related discipline.\n• Python."
    assert education(text) == ("phd", None)
    assert education("• PhD in Computer Science is a plus.")[0] is None
    assert education("• Master's or PhD degree in Computer Science.")[0] == "masters"


def test_fluency_is_read_but_only_inside_one_clause():
    from pitwall.jobsearch.normalize import languages_required

    assert languages_required("x", "Fluency in English and Dutch (ideally native)", "en") \
        == ["nl"]
    assert languages_required("x", "Fluent English required; German is nice to have.",
                              "en") == []


def test_marketing_and_resource_optimisation_are_not_optimisation_roles():
    from pitwall.jobsearch.roles import classify

    assert classify("Dutch Marketing Optimisation Specialist").labels == ("OTHER",)
    assert classify("Resource Optimization Specialist").labels == ("OTHER",)
    assert "OPTIMIZATION" in classify("Optimization Scientist").labels
    assert "OPTIMIZATION" in classify("Pricing Optimization Engineer").labels


def test_based_in_a_title_beats_the_career_fairs_address():
    from pitwall.jobsearch.normalize import normalize
    from pitwall.jobsearch.skills import vocabulary

    fields = normalize({"title": "Data Scientist (Based in Hong Kong) - Bank", "location":
                        "London, UK", "description": "Python.", "raw_json": "{}"},
                       vocab=vocabulary())
    assert json.loads(fields["countries"]) == ["HK"]


def test_borrowed_ratings_fit_the_weights_and_the_owners_ratings_test_them(repos, tmp_path):
    from pitwall.jobsearch.calibrate import MIN_EACH, calibrate, load_labels

    src = repos.job_sources.upsert("t", "job_board")
    borrowed_rows = ["cluster_id,label"]
    for i in range(2 * MIN_EACH + 10):
        good = i % 2 == 0
        job = repos.jobs.upsert(src, JobPosting(title=f"J{i}", url=f"https://t.test/{i}")).job_id
        comps = {"role": {"value": 0.9 if good else 0.2}, "company": {"value": 0.5}}
        repos.conn.execute("INSERT INTO job_matches (job_id, config_hash, passed, score, label,"
                           " components) VALUES (?, 'x', 1, 60, 'good', ?)",
                           (job, json.dumps(comps)))
        if i < 8:       # the owner rated a few of the same vacancies…
            repos.conn.execute("INSERT INTO job_review (cluster_id, feedback) VALUES (?, ?)",
                               (job, "good_match" if good else "bad_match"))
        borrowed_rows.append(f"{job},{'good' if good else 'bad'}")
    path = tmp_path / "ratings.csv"
    path.write_text("\n".join(borrowed_rows + ["999999,unsure"]), encoding="utf-8")
    labels = load_labels(str(path))
    report = calibrate(repos.conn, {"role": 50.0, "company": 50.0}, labels)
    assert (report.good, report.bad) == (4, 4)
    assert report.extra_good + report.extra_bad == len(labels) - 8, "…which count once, as theirs"
    assert report.fitted_on == "the borrowed ratings"
    assert report.blended["role"] > 50.0 > report.blended["company"]
    assert report.blended_auc >= report.current_auc


@pytest.mark.parametrize("title, other", [
    ("Associate Applied (AI) Value Engineer (DACH) - Orbit Program", True),
    ("Applied AI, Forward Deployed Machine Learning Engineer", True),
    ("Applied AI Architect, Enterprise", True),
    ("Machine Learning Engineer", False),
    ("AI Research Engineer - ML Engineering", False),
])
def test_customer_facing_titles_are_not_engineering_roles(title, other):
    from pitwall.jobsearch.roles import classify

    assert (classify(title).labels == ("OTHER",)) is other


def test_task_marketplaces_are_marked_like_agencies():
    from pitwall.jobsearch.normalize import is_agency

    assert is_agency("micro1", "")
    assert is_agency("Acme", "Compensation: $400 per accepted task.")


def test_a_title_naming_a_topic_you_avoid_is_marked_down_not_hidden():
    from pitwall.jobsearch.filters import hard_filter
    from pitwall.jobsearch.scoring import score
    from pitwall.jobsearch.settings import JobSearchConfig
    from pitwall.jobsearch.skills import vocabulary

    cfg = JobSearchConfig.model_validate({"avoid_topics": ["computer vision"]})
    job = {"title": "AI Research Engineer - Computer Vision", "role_labels":
           '["MACHINE_LEARNING"]', "skills": "[]", "countries": "[]", "cities": "[]"}
    plain = score({**job, "title": "AI Research Engineer"}, cfg, {}, vocabulary())
    marked = score(job, cfg, {}, vocabulary())
    assert plain.score - marked.score == pytest.approx(10.0)
    assert "- A topic you avoid: computer vision" in marked.minus
    assert hard_filter(job, cfg) == []


@pytest.mark.parametrize("company, text, agency", [
    ("GOLDTECH RESOURCES PTE LTD", "", True),
    ("MAESTRO HUMAN RESOURCE PTE. LTD.", "", True),
    ("THE SUPREME HR ADVISORY PTE. LTD.", "", True),
    ("Acme Pte Ltd", "EA Licence 18C9105 EA Reg R1104661", True),
    ("Acme Pte Ltd", "Contact Jane (CEI: R1104482)", True),
    ("NATIONAL UNIVERSITY OF SINGAPORE", "Apply via the NUS Career Portal (req R1234567).", False),
    ("A*STAR RESEARCH ENTITIES", "Research idea: no EA involved.", False),
])
def test_singapore_agencies_are_recognised_by_licence_and_name(company, text, agency):
    from pitwall.jobsearch.normalize import is_agency

    assert is_agency(company, text) is agency


def test_a_degree_line_under_nice_to_have_is_not_a_requirement():
    """Helsing AI Safety, 28 Sep 2026: "Nice to have • PhD in model evaluation…"."""
    from pitwall.jobsearch.normalize import education

    text = ("What you bring:\n• Python and experience deploying AI software.\n"
            "Nice to have\n• PhD in model evaluation, uncertainty quantification, or a "
            "related field, with publications in top-tier venues.")
    assert education(text)[0] is None


def test_a_phd_that_differentiates_is_not_required_beside_a_masters():
    """Robotics SWE, 28 Sep 2026: "Skills you will need • Master's degree …" then
    "Skills that will differentiate your candidacy • PhD in Robotics …"."""
    from pitwall.jobsearch.normalize import education

    text = ("Skills you will need\n• Master’s degree in Robotics, Computer Science.\n"
            "Skills that will differentiate your candidacy\n• PhD in Robotics with a focus "
            "on motion planning.")
    assert education(text)[0] in (None, "masters")
    both = "Requirements:\n• Master's degree in Computer Science.\n• PhD in Robotics."
    assert education(both)[0] == "masters"


# ───────────────────── languages with levels (Simon, 28 Sep 2026) ─────────


@pytest.mark.parametrize("typed, stored", [
    ("pt:B1", "pt:B1"), ("pt B1", "pt:B1"), ("Portuguese b1", "pt:B1"),
    ("uk native", "uk:native"), ("EN", "en"), ("it-A2", "it:A2"),
])
def test_a_language_is_stored_as_a_code_and_a_level(typed, stored):
    from pitwall.jobsearch.settings import JobSearchConfig

    assert JobSearchConfig.model_validate({"languages_spoken": [typed]}).languages_spoken \
        == [stored]


def test_an_unknown_language_is_a_loud_error():
    from pitwall.jobsearch.settings import JobSearchConfig

    with pytest.raises(ValueError):
        JobSearchConfig.model_validate({"languages_spoken": ["klingon C2"]})


@pytest.mark.parametrize("asked, cost, line", [
    ("pt", 4.0, "? Asks for Portuguese — you have B1"),
    ("it", 8.0, "- Asks for Italian (you have A2)"),
    ("de", 8.0, "- Asks for German"),
    ("ru", 0.0, None),
    ("en", 0.0, None),
])
def test_a_language_costs_what_your_level_says(asked, cost, line):
    from pitwall.jobsearch.scoring import score
    from pitwall.jobsearch.settings import JobSearchConfig
    from pitwall.jobsearch.skills import vocabulary

    cfg = JobSearchConfig.model_validate(
        {"languages_spoken": ["en:C2", "pt:B1", "it:A2", "ru:native", "uk:native"]})
    base = {"title": "Data Scientist", "role_labels": '["DATA_SCIENCE"]', "skills": "[]",
            "countries": "[]", "cities": "[]"}
    plain = score({**base, "languages_required": "[]"}, cfg, {}, vocabulary())
    asking = score({**base, "languages_required": json.dumps([asked])}, cfg, {}, vocabulary())
    assert plain.score - asking.score == pytest.approx(cost)
    if line:
        assert line in asking.minus


def test_missing_languages_are_not_reported_as_missing_skills():
    """Until v10 the language loop reused the skills list's variable, so every
    posting asking for German stored `["German"]` as its missing *skills* —
    2,401 of them — and the real missing skills were lost."""
    from pitwall.jobsearch.scoring import score
    from pitwall.jobsearch.settings import JobSearchConfig
    from pitwall.jobsearch.skills import vocabulary

    cfg = JobSearchConfig.model_validate({"languages_spoken": ["en:C2", "it:A2"]})
    job = {"title": "Data Scientist", "role_labels": '["DATA_SCIENCE"]',
           "skills": '["Kubernetes"]', "countries": "[]", "cities": "[]",
           "languages_required": '["de", "it"]'}
    m = score(job, cfg, {}, vocabulary())
    assert m.missing_languages == ["German", "Italian (you have A2)"]
    assert "German" not in m.missing_skills
    assert "Kubernetes" in m.missing_skills
    assert "- Asks for German and Italian (you have A2)" in m.minus
