"""Phase 1 of the job-search expansion: the market-wide sources.

Each adapter's parser runs against a trimmed REAL response captured on
28 Sep 2026 (tests/fixtures/jobs/), except ITJobs.pt, whose fixture is the
example in its own documentation (the API needs a key to answer at all).
Then the registry's rules, and the queries every keyword source shares.

No network: clients are scripted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pitwall.sources.jobs import RateLimited, SourceError
from pitwall.sources.jobs import arbeitnow as arbeitnow_mod
from pitwall.sources.jobs import devjobs as devjobs_mod
from pitwall.sources.jobs import eures as eures_mod
from pitwall.sources.jobs import findwork as findwork_mod
from pitwall.sources.jobs import himalayas as himalayas_mod
from pitwall.sources.jobs import itjobs as itjobs_mod
from pitwall.sources.jobs import jobicy as jobicy_mod
from pitwall.sources.jobs import jobtech as jobtech_mod
from pitwall.sources.jobs import jooble as jooble_mod
from pitwall.sources.jobs import mycareersfuture as mcf_mod
from pitwall.sources.jobs import remoteok as remoteok_mod
from pitwall.sources.jobs import workable_search as workable_mod
from pitwall.sources.jobs.search import title_matches

from .test_jobsearch_pipeline import js_project  # noqa: F401 - a fixture

FIXTURES = Path(__file__).parent / "fixtures" / "jobs"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Client:
    """Scripted responses; records every call. `answer(url, body)` decides."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def last_was_network(self):
        return True

    def set_min_delay(self, *a):
        pass

    def get_json(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.answer(url, None)

    def post_json(self, url, body, **kw):
        self.calls.append({"url": url, "body": body, **kw})
        return self.answer(url, body)


def normalized(posting):
    """What the pipeline would derive from one posting."""
    from pitwall.jobsearch.normalize import normalize
    from pitwall.jobsearch.skills import vocabulary

    row = {"title": posting.title, "location": posting.location,
           "description": posting.description, "url": posting.url,
           "raw_json": json.dumps(posting.raw), "posting_company": posting.company_name,
           "source_country": posting.country_hint,
           "source_remote": None if posting.remote is None else int(posting.remote),
           "employment_type": posting.employment_type}
    return normalize(row, vocab=vocabulary())


def _cfg(**over):
    from pitwall.jobsearch.settings import JobSearchConfig

    base = {"locations": {"countries": ["DE", "PT", "US", "GB", "SE", "SG", "NL", "CH"],
                          "home_country": "PT"},
            "work_authorization": {"needs_sponsorship": True, "authorized_countries": ["PT"]}}
    base.update(over)
    return JobSearchConfig.model_validate(base)


# ───────────────────────────────── EURES ─────────────────────────────────


def test_eures_parses_every_row_and_names_countries_not_codes():
    rows = eures_mod.parse(fixture("eures"), "machine learning")
    assert len(rows) == 3
    first = rows[0]
    assert first.url.startswith("https://europa.eu/eures/portal/jv-se/jv-details/")
    assert first.location == "Germany" and first.country_hint == "DE"
    multi = rows[1]
    assert "Portugal" in multi.location and multi.country_hint is None
    assert first.company_name and first.posted_at.startswith("2026-")


def test_eures_keeps_only_titles_with_every_word_and_stops_at_the_noise(settings):
    page = fixture("eures")

    def answer(url, body):
        return page if body["page"] == 1 else {"numberRecords": 3, "jvs": []}

    client = Client(answer)
    source = eures_mod.EuresSource(queries=["machine learning"], countries=["DE", "PT", "US"],
                                   settings=settings, client=client)
    postings = source.fetch()
    assert [p.title for p in postings] == [page["jvs"][0]["title"]]
    body = client.calls[0]["body"]
    assert body["locationCodes"] == ["de", "pt"], "the US is not a EURES member"
    assert body["sortSearch"] == "BEST_MATCH"
    assert body["keywords"][0]["specificSearchCode"] == "TITLE"
    assert source.visits[0].complete and source.visits[0].country == "*"


def test_eures_asks_for_the_smallest_window_since_the_last_visit():
    assert eures_mod.period_for(1) == "LAST_DAY"
    assert eures_mod.period_for(2) == "LAST_THREE_DAYS"
    assert eures_mod.period_for(7) == "LAST_WEEK"
    assert eures_mod.period_for(20) == "LAST_MONTH"
    assert eures_mod.period_for(60) is None


def test_title_matching_is_word_based():
    assert title_matches("Senior Machine Learning Engineer", "machine learning")
    assert not title_matches("Maschinenbediener (m/w/d)", "machine learning")
    assert title_matches("Data Scientists (all genders)", "data scientist")
    assert not title_matches("HTML Developer", "ml engineer")
    assert title_matches("ML Engineer", "ml engineer")


# ──────────────────────────── Workable search ────────────────────────────


def test_workable_search_reads_the_work_mode_and_searches_by_country_name(settings):
    rows = workable_mod.parse(fixture("workable_search"), "data scientist", "PT")
    assert rows and all(r.country_hint == "PT" for r in rows)
    hybrid = [r for r in rows if r.raw.get("workplace") == "hybrid"]
    assert hybrid and "(hybrid)" in hybrid[0].location
    source = workable_mod.WorkableSearchSource(queries=["data scientist"], countries=["PT"],
                                               settings=settings, client=object())
    assert "location=Portugal" in source.request_url("data scientist", "PT")


# ──────────────────────────────── Jooble ─────────────────────────────────


def test_jooble_parses_snippets_and_keeps_the_salary_text():
    rows = jooble_mod.parse(fixture("jooble"), "machine learning engineer")
    assert rows[0].description_truncated and rows[0].country_hint == "US"
    assert "Salary: $" in rows[0].description
    assert rows[0].url.startswith("https://jooble.org/")


def test_jooble_never_puts_its_key_in_a_cache_key_and_stops_on_429(settings):
    def answer(url, body):
        raise RateLimited("jooble: rate limited (429)")

    client = Client(answer)
    source = jooble_mod.JoobleSource("SECRET-KEY", queries=["a", "b"], countries=["US", "DE"],
                                     settings=settings, client=client)
    assert source.countries == ["US"], "the key only reaches jooble.org"
    assert source.fetch() == []
    assert len(client.calls) == 1 and source.requests_made == 1
    assert "SECRET-KEY" not in client.calls[0]["cache_key"]
    assert client.calls[0]["terms_govern"] is True


def test_jooble_asks_only_for_what_is_new_since_the_last_visit(settings):
    from datetime import UTC, datetime

    cursors = {("data scientist", "US"): {"backfilled": 1,
                                          "last_fetched_at": "2026-09-27 07:00:00"}}
    client = Client(lambda url, body: {"totalCount": 0, "jobs": []})
    source = jooble_mod.JoobleSource("k", queries=["data scientist"], countries=["US"],
                                     cursors=cursors, now=datetime(2026, 9, 28, 7, tzinfo=UTC),
                                     settings=settings, client=client)
    source.fetch()
    assert client.calls[0]["body"]["datecreatedfrom"] == "2026-09-26"


# ─────────────────────────────── ITJobs.pt ───────────────────────────────


ITJOBS_DOC_EXAMPLE = {
    "total": 2, "page": 1, "limit": 2, "query": "data",
    "results": [
        {"id": 127117, "company": {"id": 519, "name": "Hays Information Technology"},
         "title": "Data Scientist", "body": "<p>Python and SQL</p>", "salaryMin": 30000,
         "salaryMax": 40000, "workModel": 2, "types": [{"id": "1", "name": "Full Time"}],
         "contracts": [{"id": "3", "name": "Estágio profissional"}],
         "locations": [{"id": "14", "name": "Lisboa"}], "publishedAt": "2026-09-20 12:17:18",
         "slug": "data-scientist"},
        {"id": 127103, "company": {"id": 1, "name": "Babel"}, "title": "ML Engineer",
         "body": "...", "workModel": 1, "locations": [{"id": "29", "name": "International"}],
         "publishedAt": "2026-09-19 12:00:00", "slug": "ml-engineer"},
    ],
}


def test_itjobs_reads_the_documented_shape():
    lisbon, abroad = itjobs_mod.parse(ITJOBS_DOC_EXAMPLE, "data")
    assert lisbon.url == "https://www.itjobs.pt/oferta/127117/data-scientist"
    assert lisbon.location == "Lisboa, Portugal (hybrid)" and lisbon.country_hint == "PT"
    assert (lisbon.salary_min, lisbon.salary_currency) == (30000.0, "EUR")
    assert "Estágio profissional" in lisbon.employment_type
    assert abroad.remote is True and abroad.country_hint is None, "location 29 = abroad"


def test_itjobs_without_a_key_is_a_failure_not_an_empty_result(settings):
    with pytest.raises(SourceError):
        itjobs_mod.parse({"error": {"message": "API key required."}})
    with pytest.raises(ValueError):
        itjobs_mod.ITJobsSource("", queries=["data"], settings=settings, client=object())


# ─────────────────────────── JobTech, MyCareersFuture ────────────────────


def test_jobtech_links_the_ad_and_places_it_in_sweden():
    rows = jobtech_mod.parse(fixture("jobtech"), "data scientist")
    assert rows[0].url.startswith("https://arbetsformedlingen.se/")
    assert rows[0].location.endswith("Sweden") and rows[0].country_hint == "SE"
    assert rows[0].description


def test_mycareersfuture_maps_singapore_grades_and_states_the_years():
    rows = mcf_mod.parse(fixture("mycareersfuture"), "data scientist")
    first = rows[0]
    assert first.raw["_levels"] == ["mid"], "'Senior Executive' is a mid-career grade in SG"
    assert "Minimum 2 years of experience required." in first.description
    assert first.salary_min == 7500 * 12 and first.salary_currency == "SGD"
    fields = normalized(first)
    assert fields["experience_min"] == 2 and fields["experience_required"] == 1


def test_mycareersfuture_skips_overseas_postings():
    data = fixture("mycareersfuture")
    data["results"][0]["address"] = {"isOverseas": True}
    assert len(mcf_mod.parse(data)) == len(data["results"]) - 1


# ──────────────────────────────── DevJobs ────────────────────────────────


def test_devjobs_keeps_the_ml_family_and_reads_only_yes_as_sponsorship():
    data = fixture("devjobs")
    rows = devjobs_mod.parse(data, "GB")
    assert {r.raw["metaCategory"] for r in rows} == {"mlaidata"}
    assert len(rows) == 2, "the web officer and the software engineer are not kept"
    placement = next(r for r in rows if r.raw.get("expLevel") == "Junior")
    assert placement.url.startswith("https://devitjobs.uk/jobs/")
    assert normalized(placement)["experience_level"] in {"entry", "intern"}
    assert all(normalized(r)["visa_status"] == "UNKNOWN" for r in rows), \
        "'No' is the default on 2,512 of 2,514 jobs, not a refusal"
    offered = {**data[1], "hasVisaSponsorship": "Yes", "_id": "x", "jobUrl": "x"}
    [row] = devjobs_mod.parse([offered], "GB")
    assert normalized(row)["visa_status"] == "SPONSORSHIP_EXPLICIT"


# ─────────────────────────── the remote boards ───────────────────────────


def test_himalayas_restrictions_become_remote_regions():
    rows = himalayas_mod.parse(fixture("himalayas"), "machine learning")
    many, anywhere, portugal = rows
    assert "PT" in json.loads(normalized(many)["remote_regions"])
    assert json.loads(normalized(anywhere)["remote_regions"]) == ["WORLD"]
    assert json.loads(normalized(portugal)["remote_regions"]) == ["PT"]


def test_himalayas_pages_by_cursor_and_never_by_page(settings):
    pages = iter([{**fixture("himalayas"), "nextCursor": "abc"},
                  {**fixture("himalayas"), "nextCursor": None}])
    client = Client(lambda url, body: next(pages))
    source = himalayas_mod.HimalayasSource(queries=["machine learning"], work_from=["PT"],
                                           settings=settings, client=client)
    source.fetch()
    urls = [c["url"] for c in client.calls]
    assert len(urls) == 2 and "cursor=abc" in urls[1]
    assert all("page=" not in u for u in urls), "robots.txt refuses page="
    assert all("country=Portugal" in u for u in urls)


def test_jobicy_geo_decides_eligibility_for_someone_in_portugal():
    from pitwall.jobsearch.filters import hard_filter

    rows = jobicy_mod.parse(fixture("jobicy"))
    na = next(r for r in rows if "USA" in r.location)
    fields = normalized(na)
    job = {**fields, "role_labels": '["DATA_SCIENCE"]', "title": "Data Scientist"}
    assert "remote" in [r.code for r in hard_filter(job, _cfg())]


def test_remoteok_skips_its_terms_notice_and_credits_itself():
    rows = remoteok_mod.parse(fixture("remoteok"), "machine-learning")
    assert rows and all(r.url.lower().startswith("https://remoteok.com/") for r in rows)
    assert all(r.remote for r in rows)


def test_findwork_sends_its_key_as_a_header_not_in_the_url(settings):
    client = Client(lambda url, body: fixture("findwork"))
    source = findwork_mod.FindworkSource("SECRET", queries=["data scientist"],
                                         settings=settings, client=client)
    rows = source.fetch()
    call = client.calls[0]
    assert call["headers"] == {"Authorization": "Token SECRET"}
    assert "SECRET" not in call["url"] and rows


def test_arbeitnow_uk_is_the_same_parser_on_another_host(settings):
    client = Client(lambda url, body: fixture("arbeitnow_uk"))
    source = arbeitnow_mod.ArbeitnowSource(endpoint=arbeitnow_mod.ENDPOINT_UK,
                                           name="arbeitnow-uk", settings=settings,
                                           client=client)
    assert source.fetch() and client.calls[0]["url"] == arbeitnow_mod.ENDPOINT_UK


# ──────────────────────────────── queries ────────────────────────────────


def test_queries_come_from_the_profile_without_seniority_or_redundancy():
    from pitwall.jobsearch.queries import build_queries

    cfg = _cfg(roles={
        "accepted": ["MACHINE_LEARNING", "DATA_SCIENCE", "DATA_ENGINEERING"],
        "preferred_titles": ["Junior Data Scientist", "Graduate Machine Learning Engineer",
                             "Applied ML Engineer", "ML Research Engineer",
                             "Research Engineer"],
    })
    queries = build_queries(cfg)
    texts = [q.text for q in queries]
    assert "data scientist" in texts and "junior data scientist" not in texts
    assert "machine learning engineer" not in texts, "'machine learning' already finds it"
    assert "applied ml engineer" not in texts and "ml engineer" in texts
    assert "ml research engineer" not in texts and "research engineer" in texts
    assert texts[-1] == "data engineer" and not queries[-1].primary, "secondary last"


# ─────────────────────────────── the registry ────────────────────────────


def _ctx(settings, repos, cfg):
    from pitwall.jobsearch.market import Context

    return Context(cfg, settings, repos, client=object())


def test_a_missing_key_is_a_note_and_the_rest_still_build(settings, repos):
    from pitwall.jobsearch.market import market_sources

    settings.jooble_api_key_us = ""
    settings.itjobs_api_key = ""
    settings.findwork_api_key = ""
    ctx = _ctx(settings, repos, _cfg())
    names = {s.name for s in market_sources(ctx)}
    assert "jooble" not in names and "itjobs" not in names
    assert {"eures", "workable-search", "jobtech", "mycareersfuture", "himalayas"} <= names
    assert any("JOOBLE_API_KEY_US" in n for n in ctx.notes)
    assert any("ITJOBS_API_KEY" in n and "itjobs.pt/api" in n for n in ctx.notes)


def test_a_source_is_built_only_for_countries_it_covers(settings, repos):
    from pitwall.jobsearch.market import market_sources

    settings.jooble_api_key_us = "k"
    cfg = _cfg(locations={"countries": ["DE"], "home_country": "PT"})
    names = {s.name for s in market_sources(_ctx(settings, repos, cfg))}
    assert "jooble" not in names and "jobtech" not in names and "mycareersfuture" not in names
    assert "devjobs:germantechjobs.de" in names and "devjobs:devitjobs.uk" not in names


def test_remote_boards_need_remote_as_a_work_mode(settings, repos):
    from pitwall.jobsearch.market import market_sources

    cfg = _cfg(locations={"countries": ["DE"], "home_country": "PT",
                          "work_modes": ["onsite", "hybrid"]})
    names = {s.name for s in market_sources(_ctx(settings, repos, cfg))}
    assert not names & {"himalayas", "jobicy", "remoteok", "findwork"}


def test_jobicy_waits_out_its_hour(settings, repos):
    from datetime import UTC, datetime, timedelta

    from pitwall.jobsearch.market import market_sources, too_soon

    source_id = repos.job_sources.upsert("jobicy", "job_board")
    repos.job_sources.record_poll(source_id, ok=True, found=1)
    ctx = _ctx(settings, repos, _cfg())
    assert "jobicy" not in {s.name for s in market_sources(ctx)}
    assert any(n.startswith("jobicy:") for n in ctx.notes)
    later = datetime.now(UTC) + timedelta(hours=2)
    assert too_soon(repos, jobicy_mod.JobicySource(settings=settings, client=object()),
                    now=later) is None


# ─────────────────── Phase 2: the XML boards, SmartRecruiters ─────────────


def _ats_fixture(name):
    return (FIXTURES / f"ats_{name}.xml").read_text(encoding="utf-8")


def test_personio_states_its_years_and_level_where_the_rules_read_them():
    from pitwall.sources.jobs.ats import parse

    rows = parse("personio", _ats_fixture("personio"), "holidaycheck")
    assert rows[0].url == f"https://holidaycheck.jobs.personio.de/job/{rows[0].external_id}"
    assert rows[0].company_name and "_" not in rows[0].company_name
    senior = next(r for r in rows if "5-7" in (r.description or ""))
    fields = normalized(senior)
    assert fields["experience_min"] == 5 and fields["experience_required"] == 1
    assert senior.raw["_levels"] == ["experienced"]


def test_teamtailor_reads_places_and_work_mode_from_its_namespace():
    from pitwall.sources.jobs.ats import parse

    rows = parse("teamtailor", _ats_fixture("teamtailor"), "fortnoxab")
    first = rows[0]
    assert first.company_name == "Fortnox AB" and first.location.endswith("Sweden")
    assert first.remote is False and first.posted_at.startswith("2026-")


def test_a_big_smartrecruiters_board_is_paged_and_not_complete_at_the_cap(settings):
    from pitwall.sources.jobs import ats as ats_mod

    def answer(url, body):
        offset = int(url.split("offset=")[1])
        return {"totalFound": 5000, "content": [
            {"id": str(offset + i), "name": f"Role {offset + i}", "company": {"identifier": "B"}}
            for i in range(100)]}

    client = Client(answer)
    board = ats_mod.ATSSource("smartrecruiters", "BoschGroup", settings=settings, client=client)
    postings = board.fetch()
    assert len(postings) == 100 * ats_mod.SMARTRECRUITERS_MAX_PAGES
    assert board.complete_listing is False, "absence from a capped read proves nothing"

    small = Client(lambda url, body: {"totalFound": 2, "content": [
        {"id": "1", "name": "A", "company": {"identifier": "S"}},
        {"id": "2", "name": "B", "company": {"identifier": "S"}}]})
    board = ats_mod.ATSSource("smartrecruiters", "Small", settings=settings, client=small)
    assert len(board.fetch()) == 2 and board.complete_listing is True


# ─────────────────── Phase 2: boards found from posting links ─────────────


def test_board_links_are_read_from_urls_and_text_and_junk_is_ignored():
    from pitwall.jobsearch.ats_harvest import tokens_in

    text = ("Apply at https://jobs.ashbyhq.com/lovable/123 or "
            "https://boards.greenhouse.io/embed/job_board?for=monzo and "
            "https://job-boards.greenhouse.io/anthropic/jobs/1 · https://fortnoxab.teamtailor.com/jobs "
            "· https://app.recruitee.com/x · https://apply.workable.com/j/ABC "
            "· https://acme.wd3.myworkdayjobs.com/en-US/External/job/1")
    found = tokens_in(text)
    assert ("ashby", "lovable") in found and ("greenhouse", "monzo") in found
    assert ("greenhouse", "anthropic") in found and ("teamtailor", "fortnoxab") in found
    assert ("workday", "acme.wd3/External") in found
    assert not any(t in {"app", "j"} for _, t in found), "path words are not companies"
    assert tokens_in("no links here") == []


def _harvest_world(repos):
    """One aggregator posting that links to two boards, one already known."""
    src = repos.job_sources.upsert("jobtech", "job_board")
    from pitwall.sources.jobs import JobPosting

    posting = JobPosting(
        title="Data Scientist", url="https://arbetsformedlingen.se/1",
        description="Ansök: https://fortnoxab.teamtailor.com/jobs/1 · "
                    "https://boards.greenhouse.io/stripe",
        company_name="Fortnox AB")
    repos.jobs.upsert(src, posting)
    repos.conn.execute("INSERT INTO target_companies (name, ats_kind, ats_token, active)"
                       " VALUES ('Stripe', 'greenhouse', 'stripe', 1)")


def test_harvest_stores_new_boards_as_pending_and_skips_known_ones(repos):
    from pitwall.jobsearch.ats_harvest import harvest, pending

    _harvest_world(repos)
    added, top = harvest(repos.conn)
    assert added == 1 and top > 0
    [candidate] = pending(repos.conn, 10)
    assert (candidate.kind, candidate.token, candidate.company) == \
        ("teamtailor", "fortnoxab", "Fortnox AB")
    assert harvest(repos.conn, since_id=top) == (0, top), "the mark moves on"


def test_a_board_is_kept_only_if_it_belongs_to_the_employer_that_linked_it():
    from pitwall.jobsearch.ats_harvest import Candidate, judge
    from pitwall.sources.jobs import JobPosting

    theirs = [JobPosting(title="X", url="https://x.test/1", company_name="Fortnox AB")]
    other = [JobPosting(title="X", url="https://x.test/1", company_name="Someone Else")]
    assert judge(Candidate("teamtailor", "fortnoxab", "Fortnox AB"), theirs)[0] is True
    assert judge(Candidate("teamtailor", "fortnoxab", "Fortnox AB"), other)[0] is False
    assert judge(Candidate("teamtailor", "fortnoxab", None), theirs)[0] is False


@pytest.mark.usefixtures("js_project")
def test_discovery_verifies_records_and_stores_the_boards_jobs_in_one_run(settings, repos):
    from pitwall.jobsearch.run import JobSearch, JobSearchStats
    from pitwall.jobsearch.settings import load_config
    from pitwall.pipeline import Pipeline, RunStats

    _harvest_world(repos)
    rss = _ats_fixture("teamtailor")

    class FakeClient:
        cache = None

        def last_was_network(self):
            return True

        def get(self, url, **kw):
            assert "fortnoxab.teamtailor.com/jobs.rss" in url
            return rss

    search = JobSearch(Pipeline(settings, repos), load_config())
    search._client = FakeClient()
    stats = JobSearchStats()
    search.discover(search.sync_targets(stats), stats, RunStats())
    assert (stats.boards_found, stats.boards_verified) == (1, 1)
    row = repos.conn.execute("SELECT verified, postings FROM discovered_boards"
                             " WHERE token = 'fortnoxab'").fetchone()
    assert row["verified"] == 1 and row["postings"] == 2
    stored = repos.conn.execute(
        "SELECT COUNT(*) FROM jobs j JOIN job_sources s ON s.id = j.source_id"
        " WHERE s.name = 'teamtailor:fortnoxab'").fetchone()[0]
    assert stored == 2, "a verified board's jobs arrive in the run that found it"


def test_productive_boards_are_polled_every_run_and_idle_ones_weekly(repos):
    from pitwall.jobsearch.ats_harvest import boards_to_poll

    for token in ("busy", "idle", "new"):
        repos.conn.execute("INSERT INTO discovered_boards (kind, token, company, verified)"
                           " VALUES ('lever', ?, ?, 1)", (token, token))
    busy = repos.job_sources.upsert("lever:busy", "ats_json")
    idle = repos.job_sources.upsert("lever:idle", "ats_json")
    repos.conn.execute("UPDATE job_sources SET last_polled_at = datetime('now', '-1 day')"
                       " WHERE id IN (?, ?)", (busy, idle))
    from pitwall.sources.jobs import JobPosting

    job = repos.jobs.upsert(busy, JobPosting(title="ML Engineer", url="https://l.test/1")).job_id
    repos.conn.execute("INSERT INTO job_matches (job_id, config_hash, passed, score, label)"
                       " VALUES (?, 'x', 1, 70, 'good')", (job,))
    due = {b["token"] for b in boards_to_poll(repos.conn)}
    assert due == {"busy", "new"}, "idle was polled a day ago and found nothing relevant"
    assert {b["token"] for b in boards_to_poll(repos.conn, taken=[("lever", "busy")])} == {"new"}


@pytest.mark.parametrize("kind, token, company, board_names, ok", [
    ("teamtailor", "green14", "GREEN 14 AB", ["Green14"], True),
    ("teamtailor", "silex", "Silex Microsystems AB", ["Silex"], True),
    ("teamtailor", "breasmedical", "Breas Medical Aktiebolag", ["Breas Medical"], True),
    ("greenhouse", "mojangab", "Mojang AB", ["Mojang Studios"], True),
    ("teamtailor", "lead", "Radar Reticence AB", ["Lead"], False),
    ("ashby", "lovable", "Lovable Labs Sweden AB", [], True),
    ("ashby", "deeter-analytics", "Deeter Analytics", [], True),
    ("ashby", "leovegasgroup", "Gears of Leo AB", [], False),
])
def test_a_linked_board_accepts_name_variants_but_not_strangers(kind, token, company,
                                                                 board_names, ok):
    """From the first live discovery run: register names against brands, and
    tokens that are the first word of the name, all linked from the employer's
    own postings."""
    from pitwall.jobsearch.ats_harvest import Candidate, judge
    from pitwall.sources.jobs import JobPosting

    postings = [JobPosting(title="x", url="https://x.test/1", company_name=n)
                for n in board_names] or [JobPosting(title="x", url="https://x.test/1")]
    assert judge(Candidate(kind, token, company), postings)[0] is ok


def test_smartrecruiters_links_are_recorded_not_polled(repos):
    from pitwall.jobsearch.ats_harvest import harvest, pending
    from pitwall.sources.jobs import JobPosting

    src = repos.job_sources.upsert("jobtech", "job_board")
    repos.jobs.upsert(src, JobPosting(title="DS", url="https://a.test/1", company_name="AFRY",
                                      description="https://jobs.smartrecruiters.com/AFRY/1"))
    harvest(repos.conn)
    assert pending(repos.conn, 10) == []
    reason = repos.conn.execute("SELECT reason FROM discovered_boards").fetchone()[0]
    assert "LinkedInBot" in reason
