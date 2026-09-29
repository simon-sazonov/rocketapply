"""The job search end to end — stub sources, a throwaway database, no network.

§89's cases as they flow through the real pipeline: a clear pass, a clear
reject, a Junior title over a 3+ years body, silence on visas, and the same
vacancy from two sources. Then the promises the inbox makes: a second run
shows nothing new (§41), a changed posting says so (§42), a search result is
not closed for being absent from one query (§43), and saving then applying is
one application, not two (§39).
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

import pytest

from pitwall.jobsearch import tracker
from pitwall.jobsearch.companies import load_targets, sync_targets, target_rows
from pitwall.jobsearch.dedupe import compare
from pitwall.jobsearch.export import JOB_COLUMNS, applications_csv, jobs_csv
from pitwall.jobsearch.run import JobSearch
from pitwall.jobsearch.settings import load_config
from pitwall.jobsearch.store import dismiss, load_inbox, mark_seen
from pitwall.sources.jobs import JobPosting
from pitwall.sources.jobs import adzuna as adzuna_mod
from pitwall.sources.jobs import arbeitnow as arbeitnow_mod

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "jobs"

PROFILE = """
locations:
  countries: [Germany, Netherlands, France, United Kingdom, United States]
  preferred_cities: [Munich, Amsterdam]
  home_country: Portugal
roles:
  preferred_titles: [Machine Learning Engineer, Decision Scientist]
seniority:
  max_experience_years: 2
work_authorization:
  needs_sponsorship: true
  authorized_countries: [Portugal]
career_interests: [optimisation, simulation, perception]
sources:
  target_ats: false
  arbeitnow: {enabled: false}
  adzuna: {enabled: false}
"""

SKILLS = """
skills:
  - {skill: Python, proficiency: 3}
  - {skill: PyTorch, proficiency: 2}
  - {skill: SQL, proficiency: 2}
  - {skill: Machine Learning, proficiency: 3}
  - {skill: Monte Carlo, proficiency: 2}
"""

TARGETS = (
    "name,aliases,country,cities,industry,priority,careers_url,ats_kind,ats_token,"
    "sponsorship,visa_notes,tags,active\n"
    "Apex Mobility,Apex Mobility GmbH,Germany,Munich,Automotive,A,,,,unknown,,automotive,true\n"
    "Tulip Logistics,,Netherlands,Amsterdam,Logistics,B,,,,yes,sponsors,optimisation,true\n"
)


@pytest.fixture()
def js_project(project):
    config = project / "config"
    shutil.copy2(ROOT / "config" / "skills_vocab.yaml", config / "skills_vocab.yaml")
    (config / "job_search.yaml").write_text(PROFILE, encoding="utf-8")
    (config / "my_skills.yaml").write_text(SKILLS, encoding="utf-8")
    (config / "target_companies.csv").write_text(TARGETS, encoding="utf-8")
    return project


class Stub:
    """A source that returns whatever it is given."""

    def __init__(self, name, kind, postings, *, complete=False):
        self.name, self.kind, self.url = name, kind, f"https://{name}.test"
        self.complete_listing = complete
        self.postings = postings
        self.calls = 0

    def fetch(self, *, no_cache=False):
        self.calls += 1
        return list(self.postings)


class Broken(Stub):
    def fetch(self, *, no_cache=False):
        from pitwall.sources.jobs import SourceError

        raise SourceError("https://broken.test: HTTP 503")


def arbeitnow_postings():
    return arbeitnow_mod.parse(json.loads((FIXTURES / "arbeitnow.json").read_text()))


@pytest.fixture()
def search(js_project, settings, repos):
    from pitwall.pipeline import Pipeline

    pipe = Pipeline(settings, repos)
    return JobSearch(pipe, load_config())


def by_title(inbox, view="all"):
    return {c["title"]: c for c in inbox.view(view)}


# ─────────────────────────────── the parsers ─────────────────────────────


def test_arbeitnow_payload_is_parsed_with_its_remote_flag_and_job_types():
    postings = arbeitnow_postings()
    assert len(postings) == 6
    first = postings[0]
    assert first.company_name == "Apex Mobility GmbH"
    assert first.remote is False and "Entry" in first.employment_type
    assert first.posted_at.startswith("2026-")
    assert "<p>" not in first.description


def test_arbeitnow_reads_pages_until_the_cap_and_stops_on_an_empty_page(settings):
    pages = {
        arbeitnow_mod.ENDPOINT: json.loads((FIXTURES / "arbeitnow.json").read_text()),
        "https://www.arbeitnow.com/api/job-board-api?page=2": {"data": [], "links": {}},
    }

    class Client:
        def get_json(self, url, no_cache=False):
            return pages[url]

    source = arbeitnow_mod.ArbeitnowSource(max_pages=5, settings=settings, client=Client())
    assert len(source.fetch()) == 6
    assert source.complete_listing is False


def test_adzuna_strips_highlights_drops_predicted_salaries_and_marks_snippets():
    postings = adzuna_mod.parse(json.loads((FIXTURES / "adzuna.json").read_text()), "de")
    first, second = postings
    assert first.title == "Graduate Machine Learning Engineer"
    assert (first.salary_min, first.salary_max) == (55000, 65000)
    assert second.salary_min is None, "a predicted salary is Adzuna's guess, not the employer's"
    assert all(p.description_truncated and p.country_hint == "DE" for p in postings)


def test_adzuna_keeps_its_key_out_of_cache_files_and_error_messages(settings, tmp_path):
    from pitwall.sources.jobs import JobCache, PoliteClient, SourceError

    seen_keys = []

    class Client(PoliteClient):
        def get(self, url, *, no_cache=False, accept="*/*", cache_key=None, **kwargs):
            seen_keys.append(cache_key)
            raise SourceError(f"{cache_key}: HTTP 401")

    client = Client(settings, cache=JobCache(tmp_path / "c", settings=settings))
    source = adzuna_mod.AdzunaSource(
        "ID-123", "SECRET-KEY", queries=["data scientist"], countries=["DE", "GB", "FR"],
        settings=settings, client=client,
    )
    with pytest.raises(SourceError) as err:
        source.fetch()
    assert "SECRET-KEY" not in str(err.value) and "ID-123" not in str(err.value)
    assert seen_keys and all("SECRET-KEY" not in (k or "") for k in seen_keys)


def test_adzuna_plans_every_country_before_repeating_one_and_respects_its_caps(settings):
    source = adzuna_mod.AdzunaSource(
        "id", "key", queries=["a", "b"], countries=["DE", "GB", "IE", "PT"],
        max_requests=3, settings=settings, client=object(),
    )
    assert source.plan() == [("a", "de"), ("a", "gb"), ("b", "de")]
    assert source.unsupported == ["IE", "PT"]


# ────────────────────────────── the §89 cases ────────────────────────────


def test_the_spec_cases_pass_and_fail_for_the_stated_reasons(search):
    stats = search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    assert stats.seen == 6 and stats.passed == 2
    jobs = by_title(load_inbox(search.conn))

    good = jobs["Graduate Machine Learning Engineer (m/w/d)"]
    assert good["passed"] and good["visa_status"] == "SPONSORSHIP_EXPLICIT"
    assert good["target_name"] == "Apex Mobility", "matched through the target's alias"
    assert any("preferred city" in r for r in good["reasons_plus"])

    def codes(title):
        return {r["code"] for r in jobs[title]["filter_reasons"]}

    assert codes("Senior ML Engineer") == {"seniority", "experience"}
    assert codes("Junior Data Scientist") == {"experience"}, "3+ years beats 'Junior' (§4.1)"
    assert codes("AI Deployment Strategist") == {"role"}
    assert codes("Machine Learning Engineer") == {"remote", "visa"}, "remote US only (§59)"
    assert jobs["Decision Intelligence Scientist"]["passed"], "no exact-title list needed (§13)"


def test_silence_on_visas_never_filters_and_says_unclear(search):
    posting = JobPosting(title="Data Scientist", url="https://x.test/1",
                         company_name="Quiet Co", location="Amsterdam",
                         description="Python, SQL, statistics and machine learning.")
    search.run(sources=[Stub("x", "job_board", [posting])])
    job = by_title(load_inbox(search.conn))["Data Scientist"]
    assert job["passed"] and job["visa_status"] == "UNKNOWN" and job["visa_fit"] == "unknown"
    assert any("Sponsorship unclear" in r for r in job["reasons_minus"])


def test_the_owners_sponsorship_note_is_a_lead_not_a_fact(search):
    posting = JobPosting(title="Decision Scientist", url="https://x.test/2",
                         company_name="Tulip Logistics", location="Amsterdam",
                         description="Optimisation and simulation.")
    search.run(sources=[Stub("x", "job_board", [posting])])
    job = by_title(load_inbox(search.conn))["Decision Scientist"]
    assert job["visa_status"] == "LIKELY_SUPPORTED" and job["visa_fit"] == "likely"
    assert any("confirm before relying on it" in r for r in job["reasons_minus"])


def test_being_authorised_somewhere_turns_a_refusal_there_into_no_problem(js_project, search):
    (js_project / "config" / "job_search.yaml").write_text(
        PROFILE.replace("authorized_countries: [Portugal]",
                        "authorized_countries: [Portugal, United States]"),
        encoding="utf-8",
    )
    search.cfg = load_config()
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    job = by_title(load_inbox(search.conn))["Machine Learning Engineer"]
    assert job["passed"] and job["visa_fit"] == "ok"


def test_every_score_is_explained_and_bounded(search):
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    for job in load_inbox(search.conn).view("all"):
        assert 0 <= job["score"] <= 100
        assert job["label"] in {"strong", "good", "fair", "weak"}
        assert set(job["components"]) == {"role", "experience", "skills", "location", "visa",
                                          "career", "company", "fit"}
        assert job["reasons_plus"] or job["reasons_minus"], "never a bare number (§69)"


# ─────────────────────── dedupe across sources (§11) ─────────────────────


def _same_job(source_kind, url, *, truncated=False, title="Graduate Machine Learning Engineer"):
    return JobPosting(
        title=title, url=url, company_name="Apex Mobility GmbH", location="München, Bayern",
        description=("Join our perception team in Munich. 0-2 years of experience in machine "
                     "learning, Python, PyTorch" + ("…" if truncated else
                                                    " and SQL. Master's degree required.")),
        description_truncated=truncated,
    )


def test_the_same_vacancy_from_three_sources_is_one_cluster_led_by_the_ats(search):
    board = Stub("arbeitnow", "job_board", [_same_job("job_board", "https://an.test/1")])
    agg = Stub("adzuna", "aggregator_api",
               [_same_job("aggregator_api", "https://adzuna.test/1", truncated=True)])
    search.run(sources=[agg, board])
    inbox = load_inbox(search.conn)
    assert len(inbox.view("all")) == 1
    assert inbox.view("all")[0]["source_kind"] == "job_board"

    ats = Stub("greenhouse:apex", "ats_json",
               [_same_job("ats_json", "https://boards.greenhouse.io/apex/jobs/9")], complete=True)
    search.run(sources=[agg, board, ats])
    inbox = load_inbox(search.conn)
    [cluster] = inbox.view("all")
    assert cluster["source_kind"] == "ats_json", "the most direct source is the Apply link (§63)"
    assert len(cluster["members"]) == 3


def test_word_order_does_not_make_a_second_vacancy():
    a = {"source_id": 1, "company_norm": "apex mobility", "canonical_url": "a",
         "title_key": "engineer graduate learning machine", "countries": '["DE"]',
         "description": "same text", "title_norm": "graduate machine learning engineer"}
    b = {**a, "source_id": 2, "canonical_url": "b",
         "title_norm": "machine learning engineer graduate"}
    assert compare(a, b) == "strong"


def test_an_uncertain_match_is_flagged_not_merged(search):
    first = JobPosting(title="Machine Learning Engineer", url="https://a.test/1",
                       company_name="Apex Mobility", location="Munich", description="ML.")
    second = JobPosting(title="Machine Learning Engineer, Perception", url="https://b.test/1",
                        company_name="Apex Mobility", location="Munich", description="ML.")
    search.run(sources=[Stub("a", "job_board", [first]), Stub("b", "aggregator_api", [second])])
    clusters = load_inbox(search.conn).view("all")
    assert len(clusters) == 2
    assert any(c["possible_duplicate_of"] for c in clusters)


def test_two_postings_from_one_source_are_never_merged(search):
    twins = [JobPosting(title="Data Scientist", url=f"https://a.test/{i}",
                        company_name="Apex Mobility", location="Munich",
                        description="Python and ML.") for i in (1, 2)]
    search.run(sources=[Stub("a", "ats_json", twins, complete=True)])
    assert len(load_inbox(search.conn).view("all")) == 2


# ──────────────────── new, changed, closed (§41-43) ──────────────────────


def test_a_second_run_brings_nothing_new_and_seen_jobs_leave_the_inbox(search):
    source = Stub("arbeitnow", "job_board", arbeitnow_postings())
    search.run(sources=[source])
    first = load_inbox(search.conn)
    assert len(first.view("new")) == 2
    stats = search.run(sources=[source])
    assert stats.new == 0 and stats.clustered == 0
    inbox = load_inbox(search.conn)
    mark_seen(search.conn, [c["cluster_id"] for c in inbox.view("new")])
    assert load_inbox(search.conn).view("new") == []
    assert len(load_inbox(search.conn).view("matches")) == 2


def test_a_changed_posting_comes_back_to_the_inbox_marked_changed(search):
    posting = JobPosting(title="Data Scientist", url="https://x.test/1", company_name="Apex",
                         location="Munich", description="Python and ML.")
    source = Stub("x", "job_board", [posting])
    search.run(sources=[source])
    [job] = load_inbox(search.conn).view("new")
    mark_seen(search.conn, [job["cluster_id"]])
    search.conn.execute("UPDATE job_review SET seen_at = datetime('now', '-1 hour')")
    source.postings = [JobPosting(title="Data Scientist", url="https://x.test/1",
                                  company_name="Apex", location="Munich",
                                  description="Python, ML and now Kubernetes.")]
    stats = search.run(sources=[source])
    assert stats.updated == 1
    [job] = load_inbox(search.conn).view("new")
    assert job["changed_since_seen"] and job["previous_description"] == "Python and ML."


def test_a_search_source_does_not_close_what_one_query_did_not_return(search):
    posting = JobPosting(title="Data Scientist", url="https://x.test/1", company_name="Apex",
                         location="Munich", description="Python.")
    search.run(sources=[Stub("adzuna", "aggregator_api", [posting])])
    search.run(sources=[Stub("adzuna", "aggregator_api", [])])
    assert load_inbox(search.conn).view("all")[0]["status"] == "open"

    search.run(sources=[Stub("ats", "ats_json", [posting], complete=True)])
    search.run(sources=[Stub("ats", "ats_json", [], complete=True)])
    statuses = {r["status"] for r in search.conn.execute("SELECT status FROM jobs")}
    assert "closed" in statuses, "a complete listing proves absence"


def test_a_failing_source_is_recorded_and_the_rest_still_run(search):
    good = Stub("arbeitnow", "job_board", arbeitnow_postings())
    stats = search.run(sources=[Broken("broken", "job_board", []), good])
    assert stats.sources_failed == 1 and stats.seen == 6
    health = {s["name"]: s for s in search.repos.job_sources.all()}
    assert health["broken"]["last_error"].endswith("HTTP 503")
    assert health["arbeitnow"]["last_status"] == "ok" and health["arbeitnow"]["jobs_found"] == 6


# ───────────────────────── tracker and export ────────────────────────────


def test_saving_then_applying_is_one_application_with_a_history(search):
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    job = by_title(load_inbox(search.conn))["Graduate Machine Learning Engineer (m/w/d)"]
    app_id = tracker.from_cluster(search.conn, job, "SHORTLISTED")
    assert tracker.from_cluster(search.conn, job, "APPLIED") == app_id
    app = tracker.get(search.conn, app_id)
    assert app["status"] == "APPLIED" and app["date_applied"]
    assert [e["to_status"] for e in tracker.events(search.conn, app_id)] == \
        ["SHORTLISTED", "APPLIED"]
    inbox = load_inbox(search.conn)
    assert by_title(inbox, "all")[job["title"]]["application"]["id"] == app_id
    assert job["title"] not in by_title(inbox, "new")


def test_an_application_elsewhere_warns_on_every_job_at_that_company(search):
    tracker.create_manual(search.conn, company="Apex Mobility AG", title="Data Engineer",
                          date_applied="2026-09-18")
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    job = by_title(load_inbox(search.conn))["Graduate Machine Learning Engineer (m/w/d)"]
    [earlier] = job["company_applications"]
    assert earlier["title"] == "Data Engineer" and earlier["date_applied"] == "2026-09-18"


def test_dismissing_a_vacancy_survives_it_arriving_from_another_source(search):
    board = Stub("arbeitnow", "job_board", [_same_job("job_board", "https://an.test/1")])
    search.run(sources=[board])
    [cluster] = load_inbox(search.conn).view("new")
    dismiss(search.conn, cluster["cluster_id"], "Not interested")
    agg = Stub("adzuna", "aggregator_api",
               [_same_job("aggregator_api", "https://adzuna.test/1", truncated=True)])
    search.run(sources=[board, agg])
    inbox = load_inbox(search.conn)
    assert inbox.view("new") == [] and len(inbox.view("dismissed")) == 1


def test_the_csv_has_the_spec_columns_and_one_row_per_vacancy(search):
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    rows = list(csv.DictReader(io.StringIO(jobs_csv(search.conn, "all"))))
    assert len(rows) == 6
    assert list(rows[0])[:19] == list(JOB_COLUMNS[:19])
    tracker.create_manual(search.conn, company="Apex", title="Data Scientist")
    apps = list(csv.DictReader(io.StringIO(applications_csv(search.conn))))
    assert apps[0]["status"] == "APPLIED" and apps[0]["history"].startswith("APPLIED")


# ───────────────────── the target list and the people sweep ──────────────


def test_target_companies_validate_and_link_to_the_people_list(js_project, repos):
    (js_project / "config" / "target_companies.csv").write_text(
        TARGETS + "McLaren Racing,,United Kingdom,Woking,Motorsport,Z,,notanats,x,maybe,,,true\n",
        encoding="utf-8",
    )
    targets, report = load_targets()
    assert len(targets) == 3
    messages = " ".join(i.message for i in report.issues)
    assert "priority" in messages and "ats_kind" in messages and "sponsorship" in messages
    from pitwall.inputs import load_and_sync

    load_and_sync(repos.conn)
    sync_targets(repos.conn, targets)
    rows = {r["name"]: r for r in target_rows(repos.conn)}
    assert rows["McLaren Racing"]["networking_company_id"] is not None
    assert rows["Apex Mobility"]["networking_company_id"] is None


def test_a_company_removed_from_the_csv_is_kept_but_inactive(js_project, repos):
    targets, _ = load_targets()
    sync_targets(repos.conn, targets)
    sync_targets(repos.conn, targets[:1])
    names = {r["name"] for r in target_rows(repos.conn)}
    assert names == {targets[0].name}
    assert repos.conn.execute("SELECT COUNT(*) FROM target_companies").fetchone()[0] == 2


def test_job_targets_never_become_people_search_targets(search):
    from pitwall.inputs import load_and_sync

    companies, _, _ = load_and_sync(search.conn)
    before = sorted(c.name for c in companies)
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    companies, _, _ = load_and_sync(search.conn)
    assert sorted(c.name for c in companies) == before == ["McLaren Racing"]
    assert search.conn.execute("SELECT COUNT(*) FROM target_companies").fetchone()[0] == 2


def test_saving_the_target_list_keeps_its_documentation(js_project):
    from pitwall.jobsearch.companies import save_targets

    path = js_project / "config" / "target_companies.csv"
    path.write_text("# what each column means\n" + TARGETS, encoding="utf-8")
    targets, _ = load_targets()
    targets[0].ats_kind, targets[0].ats_token = "greenhouse", "apex"
    save_targets(targets)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# what each column means\nname,")
    reloaded, report = load_targets()
    assert report.ok and reloaded[0].ats_token == "apex"


# ───────────────── ATS discovery must not adopt a stranger's board ───────


def _board(*pairs):
    return [JobPosting(title=t, url=f"https://b.test/{i}", company_name=c)
            for i, (t, c) in enumerate(pairs)]


@pytest.mark.parametrize(("target", "token", "postings", "ok"), [
    # the four boards the first live run adopted, 22 Sep 2026 — all wrong
    ("Google DeepMind|DeepMind", "google",
     _board(("Senior Marketer (Sample)", "Google")), False),
    ("Red Bull Racing|Oracle Red Bull Racing", "redbull",
     _board(("Senior Marketer (Sample)", "Fiture Marketing")), False),
    ("Aleph Alpha", "aleph", _board(("Client Partner - Spotify Ads", None)), False),
    ("Novo Nordisk", "novo", _board(("Senior Product Manager, Banking", None)), False),
    # and the ones that were right
    ("Wayve|Wayve Technologies", "wayve", _board(("ML Engineer", "Wayve")), True),
    ("Spotify", "spotify", _board(("Data Scientist", None)), True),
    ("Sword Health|Sword", "swordhealth", _board(("AI Engineer", "Sword")), True),
])
def test_a_board_is_only_adopted_with_evidence_it_is_the_companys(target, token, postings, ok):
    from pitwall.jobsearch.companies import verify_board

    name, *aliases = target.split("|")
    verdict, why = verify_board(name, aliases, token, postings)
    assert verdict is ok, why


@pytest.mark.parametrize(("company", "token", "postings", "ok"), [
    # the people-search list (config/companies.csv) goes through the same check
    ("PREMA Racing|PREMA Powerteam", "prema", _board(("Race Engineer", None)), False),
    ("Toyota Gazoo Racing|TGR-E|Toyota Gazoo Racing Europe", "toyota",
     _board(("Sales Associate", "Toyota Motor North America")), False),
    ("Trident|Trident Motorsport", "trident", _board(("Senior Marketer (Sample)", None)), False),
    # its first live run, 22 Sep 2026: this board is Genesis Global, capital-markets software
    ("Genesis Magma Racing|Genesis|Hyundai Motorsport", "genesis",
     _board(("Head of DevOps", None)), False),
    ("Manthey|Manthey Racing|Manthey EMA", "manthey", _board(("Data Engineer", None)), True),
    ("Hitech TGR|Hitech Grand Prix|Hitech Pulse-Eight", "hitechgp",
     _board(("Performance Engineer", "Hitech Grand Prix")), True),
])
def test_a_people_search_board_needs_the_same_evidence(company, token, postings, ok):
    """A fragment of a team's name on a board that names nobody, a namesake
    elsewhere in the group, and a placeholder board are all turned down — and
    so is a nameless board at an alias, because those aliases are kept for
    recall. An alias still counts when the postings themselves name it."""
    from pitwall.jobsearch.companies import verify_board

    name, *aliases = company.split("|")
    verdict, why = verify_board(name, aliases, token, postings, alias_tokens=False)
    assert verdict is ok, why


def test_an_alias_vouches_for_a_nameless_board_only_when_allowed_to():
    from pitwall.jobsearch.companies import verify_board

    board = _board(("Head of DevOps", None))
    aliases = ["Genesis", "Hyundai Motorsport"]
    assert verify_board("Genesis Magma Racing", aliases, "genesis", board)[0]
    ok, why = verify_board("Genesis Magma Racing", aliases, "genesis", board,
                           alias_tokens=False)
    assert not ok and "only an alias" in why


def test_new_since_last_run_counts_only_what_the_latest_run_found(search):
    source = Stub("arbeitnow", "job_board", arbeitnow_postings())
    search.run(sources=[source])
    assert load_inbox(search.conn).counts()["new_since_last_run"] == 2
    search.conn.execute("UPDATE jobs SET first_seen_at = datetime('now', '-1 day')")
    search.conn.execute("UPDATE runs SET started_at = datetime('now', '-1 day')")
    search.run(sources=[source])
    assert load_inbox(search.conn).counts()["new_since_last_run"] == 0


def test_a_people_search_nickname_does_not_claim_an_open_market_posting(search, write_inputs):
    """"Genesis" is an alias of Genesis Magma Racing for the LinkedIn sweep. A
    Paris start-up called Genesis posting on Arbeitnow is not that team."""
    from pitwall.export import openings_frame
    from pitwall.inputs import load_and_sync
    from pitwall.models import Company, Role

    write_inputs(
        [Company(name="Genesis Magma Racing", aliases="Genesis|Hyundai Motorsport", tier=1),
         Company(name="Apex Racing", tier=1)],
        [Role(title="Data Scientist", family="data_science")],
    )
    load_and_sync(search.conn)
    postings = [
        JobPosting(title="Simulation Engineer", url="https://a.test/1", company_name="Genesis",
                   location="Paris", description="Physics simulation."),
        JobPosting(title="Data Scientist", url="https://a.test/2", company_name="Apex Racing",
                   location="Munich", description="Python."),
    ]
    search.run(sources=[Stub("arbeitnow", "job_board", postings)])
    links = {r["title"]: r["company_id"] for r in search.conn.execute(
        "SELECT title, company_id FROM jobs")}
    assert links["Simulation Engineer"] is None, "an alias is not enough"
    assert links["Data Scientist"] is not None, "the canonical name is"
    titles = [r["title"] for r in openings_frame(search.conn)]
    assert titles == ["Data Scientist"], "Openings shows only the linked one"
