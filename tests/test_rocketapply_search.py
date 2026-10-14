"""RocketApply's search box: one SQL statement per keystroke, same answers as the inbox.

`load_inbox` is the reference: it reads every vacancy into Python and has
been right for a month. The fast search must list exactly the vacancies the
inbox's "matches" view does, represented by exactly the same posting, without
ever reading a description.
"""

from __future__ import annotations

import pytest

from pitwall.jobsearch import store, tracker
from pitwall.jobsearch.store import (
    JobQuery,
    job_detail,
    load_inbox,
    search_facets,
    search_ids,
    search_jobs,
    stage_counts,
)

from .test_jobsearch_pipeline import (  # noqa: F401, TID252
    Stub,
    _same_job,
    arbeitnow_postings,
    js_project,
    search,
)


@pytest.fixture()
def searched(search):  # noqa: F811 - the imported fixture, requested by name
    """The Arbeitnow fixture plus one vacancy three sources carry."""
    store._SHADOW.update(key=None, value=None)
    board = Stub("arbeitnow", "job_board",
                 arbeitnow_postings() + [_same_job("job_board", "https://an.test/1")])
    agg = Stub("adzuna", "aggregator_api",
               [_same_job("aggregator_api", "https://adzuna.test/1", truncated=True)])
    ats = Stub("greenhouse:apex", "ats_json",
               [_same_job("ats_json", "https://boards.greenhouse.io/apex/jobs/9")], complete=True)
    search.run(sources=[agg, board, ats])
    return search.conn


def test_the_fast_search_lists_what_the_inbox_lists(searched):
    inbox = load_inbox(searched)
    page = search_jobs(searched, JobQuery(), limit=1000)
    assert page.total == len(inbox.view("matches")) == len(page.rows)
    assert {r["cluster_id"] for r in page.rows} == {c["cluster_id"] for c in inbox.view("matches")}
    assert {r["id"] for r in page.rows} == {c["id"] for c in inbox.view("matches")}, \
        "the same posting represents each vacancy"
    everything = search_jobs(searched, JobQuery(include_filtered=True), limit=1000)
    assert {r["id"] for r in everything.rows} == \
        {c["id"] for c in inbox.view("matches") + inbox.view("filtered")}


def test_a_vacancy_three_sources_carry_is_one_row_led_by_the_ats(searched):
    rows = [r for r in search_jobs(searched, JobQuery(include_filtered=True), limit=1000).rows
            if r["title"] == "Graduate Machine Learning Engineer"]
    assert len(rows) == 1
    detail = job_detail(searched, rows[0]["id"])
    assert detail["source_kind"] == "ats_json", "the most direct source is the Apply link"
    # The detail panel lists the same sources the inbox groups under this vacancy
    # (which copies merge is dedupe's business, tested with it).
    inbox = {c["cluster_id"]: c for c in load_inbox(searched).view("all")}
    expected = {m["url"] for m in inbox[detail["cluster_id"]]["members"]}
    assert {m["url"] for m in detail["members"]} >= expected
    assert len(detail["members"]) >= 2
    assert detail["members"][0]["source"] == "greenhouse:apex", "most direct source first"
    assert detail["description"], "the detail panel is where the description is read"


def test_results_never_carry_a_description(searched):
    row = search_jobs(searched, JobQuery(include_filtered=True), limit=5).rows[0]
    assert "description" not in row and "previous_description" not in row
    assert isinstance(row["reasons_plus"], list), "JSON columns arrive parsed"


def test_results_are_ranked_by_score_and_paged(searched):
    q = JobQuery(include_filtered=True)
    full = search_jobs(searched, q, limit=1000).rows
    scores = [r["score"] for r in full]
    assert scores == sorted(scores, reverse=True)
    assert [r["id"] for r in search_jobs(searched, q, limit=2, offset=1).rows] == \
        [r["id"] for r in full[1:3]]
    assert search_ids(searched, q, limit=3) == [r["id"] for r in full[:3]]


def test_every_word_must_match_the_title_company_place_or_skills(searched):
    q = JobQuery(text="machine learning", include_filtered=True)
    rows = search_jobs(searched, q, limit=100).rows
    assert rows
    for row in rows:
        d = job_detail(searched, row["id"])
        haystack = " ".join([d["title"], d["company"], d.get("location") or "",
                             " ".join(d["skills"])]).lower()
        assert "machine" in haystack and "learning" in haystack, d["title"]
    assert "Senior ML Engineer" in {r["title"] for r in rows}, "found through its skills"
    assert search_jobs(searched, JobQuery(text="zzzz nothing", include_filtered=True)).total == 0
    # A % in the box is a character, not a wildcard.
    assert search_jobs(searched, JobQuery(text="%", include_filtered=True)).total == 0


@pytest.mark.parametrize("field, value, check", [
    ("countries", ("DE",), lambda r: "DE" in r["countries"]),
    ("work_modes", ("remote",), lambda r: r["remote_status"] == "remote"),
    ("min_score", 50.0, lambda r: r["score"] >= 50),
])
def test_each_filter_keeps_only_what_it_says(searched, field, value, check):
    rows = search_jobs(searched, JobQuery(include_filtered=True, **{field: value}), limit=1000).rows
    assert all(check(r) for r in rows)


def test_a_facet_leaves_out_its_own_filter(searched):
    q = JobQuery(include_filtered=True)
    all_countries = search_facets(searched, q)["countries"]
    narrowed = search_facets(searched, JobQuery(include_filtered=True, countries=("DE",)))
    assert narrowed["countries"] == all_countries, "choosing Germany must not zero the rest"
    de = search_jobs(searched, JobQuery(include_filtered=True, countries=("DE",))).total
    assert sum(narrowed["work_modes"].values()) == de, "other facets do narrow"


def test_hide_tracked_drops_what_is_already_in_the_pipeline(searched):
    q = JobQuery(include_filtered=True)
    first = search_jobs(searched, q, limit=1).rows[0]
    tracker.ensure_for_job(searched, first["id"])
    assert first["id"] not in {r["id"] for r in search_jobs(
        searched, JobQuery(include_filtered=True, hide_tracked=True), limit=1000).rows}
    row = next(r for r in search_jobs(searched, q, limit=1000).rows if r["id"] == first["id"])
    assert row["application_status"] == "SHORTLISTED"


def test_hide_tracked_drops_closed_but_not_removed(searched):
    """A job closed out before you applied is done with; one you took off the
    selection yourself may be wanted again."""
    q = JobQuery(include_filtered=True, include_closed=True, hide_tracked=True)
    closed, removed = search_ids(searched, JobQuery(include_filtered=True), limit=2)
    tracker.set_status(searched, tracker.ensure_for_job(searched, closed), "CLOSED")
    [app_id] = tracker.select_jobs(searched, [removed])
    tracker.skip(searched, app_id)
    shown = {r["id"] for r in search_jobs(searched, q, limit=1000).rows}
    assert closed not in shown and removed in shown


def test_the_process_strip_counts_each_stage(searched):
    ids = search_ids(searched, JobQuery(include_filtered=True), limit=3)
    tracker.select_jobs(searched, ids)
    tracker.record_generation(searched, job_id=ids[0], directory="output/x")
    counts = stage_counts(searched)
    assert counts["found"] == search_jobs(searched, JobQuery()).total
    assert (counts["selected"], counts["docs_ready"], counts["in_process"], counts["applied"]) \
        == (2, 1, 0, 0)
