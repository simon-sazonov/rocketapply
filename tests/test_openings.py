"""The jobs × people join — §2.5, the point of Phase 2.

Everything else in the phase is plumbing for one sentence:

    "Alpine posted a Performance Engineer role 3 days ago — you already have
     4 people in that department, none contacted."

So this file is mostly about ordering and attribution: which openings surface
first, which people hang off them, and in what order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pitwall.export import openings_frame, role_family_for, role_proximity
from pitwall.inputs import sync_to_db
from pitwall.models import Score
from pitwall.sources.jobs import JobPosting


@pytest.fixture()
def seeded(repos, settings, company, role):
    """One company, three people at it, and a CRM row each."""
    sync_to_db(repos.conn, [company], [role])
    comp = repos.companies.all()[0]
    rol = repos.roles.all()[0]
    conn = repos.conn
    conn.execute(
        "INSERT INTO queries (id, company_id, role_id, template_id, query_text,"
        " query_hash, status) VALUES (1,?,?,'profile_exact','q','h','done')",
        (comp["id"], rol["id"]),
    )
    people = [
        ("jane", "Jane Doe", "Senior Data Scientist", 88.0, "new"),
        ("sam", "Sam Patel", "Race Strategy Engineer", 71.0, "contacted"),
        ("kim", "Kim Alvarez", "Mechanical Technician", 44.0, "new"),
    ]
    ids = []
    for i, (slug, name, title, score, status) in enumerate(people, start=1):
        cid, _ = repos.candidates.upsert({
            "linkedin_slug": slug, "profile_url": f"https://www.linkedin.com/in/{slug}",
            "full_name": name, "current_title": title, "current_company": comp["name"],
        })
        conn.execute(
            "INSERT INTO search_results (id, query_id, provider, rank, url, canonical_url,"
            " title, snippet, processed) VALUES (?,1,'searxng',?,?,?,?,?,1)",
            (i, i, f"https://www.linkedin.com/in/{slug}", f"https://linkedin.com/in/{slug}",
             f"{name} - {title} - {comp['name']}", f"{title} at {comp['name']}"),
        )
        repos.candidates.link_source(cid, i)
        repos.scores.upsert(Score(
            candidate_id=cid, company_id=comp["id"], role_id=rol["id"],
            heuristic_score=score, llm_score=score, final_score=score,
            verdict="strong" if score >= 75 else "possible",
            prompt_version=settings.prompt_version,
        ))
        repos.crm.ensure(cid)
        repos.crm.update(cid, status=status)
        ids.append(cid)
    return {"company": comp, "role": rol, "ids": ids}


def post(repos, company_id, title, url, *, days_ago=3, location="Woking, England",
         source="greenhouse:apex"):
    source_id = repos.job_sources.upsert(source, "ats_json")
    when = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    job = JobPosting(title=title, url=url, posted_at=when, location=location)
    return repos.jobs.upsert(source_id, job, company_id)[0], source_id


# ─────────────────────────── role proximity ──────────────────────────────


def test_a_job_title_is_classified_with_the_same_table_as_a_person():
    assert role_family_for("Senior Data Scientist") == "data_science"
    assert role_family_for("Race Strategy Engineer") == "strategy"
    assert role_family_for("Hospitality Coordinator") is None


def test_the_same_discipline_ranks_above_a_different_one():
    assert role_proximity("Data Scientist", "data_science", "Data Scientist") == 2
    assert role_proximity("Data Scientist", "strategy", "Race Strategist") == 1
    assert role_proximity("Office Manager", None, None) == 0


def test_a_persons_own_title_beats_the_family_the_query_was_for():
    """The stored family says what we searched for, not what the person does.

    A mechanic surfaced by a "Data Scientist" query is not in the data
    department, and counting them as one would put the wrong name under the
    wrong vacancy.
    """
    assert role_proximity("Data Scientist", "operations", "Machine Learning Engineer") == 2
    assert role_proximity("Data Scientist", "data_science", "Mechanical Technician") == 0
    assert role_proximity("Data Scientist", "data_science", None) == 2


# ──────────────────────── the join, and its ordering ─────────────────────


def test_an_opening_carries_the_people_you_already_have(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist",
         "https://apex.test/jobs/1")
    rows = openings_frame(repos.conn, settings)

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Data Scientist"
    assert row["company_name"] == seeded["company"]["name"]
    assert row["people_count"] == 3
    assert row["age_days"] == 3


def test_people_are_ranked_by_discipline_then_by_score(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    people = openings_frame(repos.conn, settings)[0]["people"]

    assert people[0]["name"] == "Jane Doe", "the data scientist comes first"
    assert people[0]["proximity"] == "same role family"
    assert [p["score"] for p in people] == sorted(
        (p["score"] for p in people), key=lambda s: -s
    ) or people[0]["proximity_rank"] >= people[-1]["proximity_rank"]


def test_the_uncontacted_count_is_what_makes_the_sentence_work(repos, settings, seeded):
    """"…4 people in that department, none contacted" — that clause."""
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    row = openings_frame(repos.conn, settings)[0]
    assert row["uncontacted_count"] == 2       # Sam is already contacted
    assert row["same_family_count"] == 1


def test_openings_where_you_know_someone_in_the_discipline_come_first(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1",
         days_ago=30)
    post(repos, None, "Head of Catering", "https://other.test/jobs/2", days_ago=1)
    rows = openings_frame(repos.conn, settings)
    assert [r["title"] for r in rows] == ["Data Scientist", "Head of Catering"]
    assert rows[1]["people_count"] == 0


def test_a_posting_for_an_unknown_company_is_kept_not_dropped(repos, settings, seeded):
    """company_id is nullable on purpose — losing the posting is worse."""
    post(repos, None, "Aerodynamicist", "https://unknown.test/jobs/9")
    rows = openings_frame(repos.conn, settings)
    titles = [r["title"] for r in rows]
    assert "Aerodynamicist" in titles


def test_ignored_people_stay_out_of_the_join(repos, settings, seeded):
    repos.crm.update(seeded["ids"][2], status="ignored")
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    row = openings_frame(repos.conn, settings)[0]
    assert row["people_count"] == 2
    assert "Kim Alvarez" not in [p["name"] for p in row["people"]]


def test_a_minimum_score_filters_the_people_not_the_openings(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    rows = openings_frame(repos.conn, settings, min_score=80.0)
    assert len(rows) == 1
    assert [p["name"] for p in rows[0]["people"]] == ["Jane Doe"]


def test_filtering_to_one_company(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    post(repos, None, "Something Else", "https://other.test/jobs/2")
    assert len(openings_frame(repos.conn, settings, company=seeded["company"]["name"])) == 1


def test_closed_postings_do_not_appear(repos, settings, seeded):
    job_id, source_id = post(repos, seeded["company"]["id"], "Data Scientist",
                             "https://apex.test/jobs/1")
    assert len(openings_frame(repos.conn, settings)) == 1
    repos.jobs.close_missing(source_id, [])
    assert openings_frame(repos.conn, settings) == []


def test_an_empty_database_returns_nothing_rather_than_failing(repos, settings):
    assert openings_frame(repos.conn, settings) == []


# ─────────────────── closed, not deleted (§2.2 / Appendix C) ─────────────


def test_a_vanished_posting_is_closed_and_kept(repos, settings, seeded):
    job_id, source_id = post(repos, seeded["company"]["id"], "Data Scientist",
                             "https://apex.test/jobs/1")
    closed = repos.jobs.close_missing(source_id, [])
    assert closed == 1

    row = repos.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row is not None, "the row survives — 'you missed this one' is history"
    assert row["status"] == "closed"
    assert row["first_seen_at"]


def test_a_posting_that_comes_back_reopens(repos, settings, seeded):
    job_id, source_id = post(repos, seeded["company"]["id"], "Data Scientist",
                             "https://apex.test/jobs/1")
    repos.jobs.close_missing(source_id, [])
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    row = repos.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "open"


def test_seeing_a_posting_again_updates_rather_than_duplicates(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    post(repos, seeded["company"]["id"], "Senior Data Scientist", "https://apex.test/jobs/1")
    rows = repos.jobs.open_jobs()
    assert len(rows) == 1
    assert rows[0]["title"] == "Senior Data Scientist"


def test_first_seen_at_survives_an_update(repos, settings, seeded):
    job_id, _ = post(repos, seeded["company"]["id"], "Data Scientist",
                     "https://apex.test/jobs/1")
    before = repos.conn.execute(
        "SELECT first_seen_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["first_seen_at"]
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    after = repos.conn.execute(
        "SELECT first_seen_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["first_seen_at"]
    assert before == after


def test_the_same_url_on_two_sources_is_two_rows(repos, settings, seeded):
    """UNIQUE(source_id, url): the aggregator and the ATS can both carry it."""
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1",
         source="greenhouse:apex")
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1",
         source="motorsportjobs")
    assert len(repos.jobs.open_jobs()) == 2


# ─────────────────────────── the polling stage ───────────────────────────


def test_a_failing_source_is_recorded_and_the_run_continues(pipeline, repos, monkeypatch):
    """§2.3: "A failing adapter is non-fatal"."""
    from pitwall.pipeline import RunParams, RunStats
    from pitwall.sources.jobs import SourceError

    class Dead:
        name, kind = "dead-source", "aggregator"
        url = "https://dead.test/sitemap.xml"

        def fetch(self, **kw):
            raise SourceError("403 refused")

    class Alive:
        name, kind = "live-source", "ats_json"
        url = "https://live.test"
        company_id = None

        def fetch(self, **kw):
            return [JobPosting(title="Race Engineer", url="https://live.test/j/1")]

    monkeypatch.setattr("pitwall.sources.jobs.build_sources", lambda *a, **k: [Dead(), Alive()])
    stats = pipeline.poll_jobs(RunParams(), RunStats())

    assert stats.job_sources_failed == 1
    assert stats.jobs_new == 1, "the healthy source still ran"
    assert any("403" in n for n in stats.notes)
    assert repos.job_sources.get("dead-source")["last_status"].startswith("403")
    assert repos.job_sources.get("live-source")["last_status"] == "ok"


def test_a_posting_is_linked_to_a_company_by_name_or_alias(pipeline, repos, monkeypatch):
    from pitwall.pipeline import RunParams, RunStats

    class Source:
        name, kind = "agg", "aggregator"
        url = "https://a.test/s.xml"

        def fetch(self, **kw):
            return [
                JobPosting(title="A", url="https://a.test/1", company_name="McLaren Racing"),
                JobPosting(title="B", url="https://a.test/2", company_name="McLaren F1"),
                JobPosting(title="C", url="https://a.test/3", company_name="Nobody Ltd"),
            ]

    monkeypatch.setattr("pitwall.sources.jobs.build_sources", lambda *a, **k: [Source()])
    pipeline.poll_jobs(RunParams(), RunStats())

    rows = {r["title"]: r for r in repos.jobs.open_jobs()}
    assert rows["A"]["company_id"] is not None, "canonical name"
    assert rows["B"]["company_id"] == rows["A"]["company_id"], "alias"
    assert rows["C"]["company_id"] is None, "unknown companies are kept, unlinked"


def test_nothing_configured_says_so_rather_than_failing(pipeline, monkeypatch):
    from pitwall.pipeline import RunParams, RunStats

    monkeypatch.setattr("pitwall.sources.jobs.build_sources", lambda *a, **k: [])
    stats = pipeline.poll_jobs(RunParams(), RunStats())
    assert stats.jobs_seen == 0
    assert any("no job sources configured" in n for n in stats.notes)


def test_a_missing_title_is_a_float_not_a_string(repos, settings, seeded):
    """pandas represents a NULL title as NaN, and 147 of the 384 people in the
    real database have no title. Found by running this against them."""
    import math

    assert role_family_for(float("nan")) is None
    assert role_family_for(None) is None
    assert role_proximity("Data Scientist", "data_science", math.nan) == 2
    assert role_proximity("Data Scientist", math.nan, math.nan) == 0

    repos.conn.execute("UPDATE candidates SET current_title = NULL")
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    row = openings_frame(repos.conn, settings)[0]
    assert row["people_count"] == 3
    assert row["same_family_count"] == 3, "falls back to the family the query was for"


def test_a_null_title_never_renders_as_the_string_nan(repos, settings, seeded):
    repos.conn.execute("UPDATE candidates SET current_title = NULL, location = NULL")
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    people = openings_frame(repos.conn, settings)[0]["people"]
    assert all(p["title"] is None for p in people)
    assert all(isinstance(p["name"], str) and p["name"] != "nan" for p in people)


def test_the_uncontacted_clause_counts_within_the_discipline(repos, settings, seeded):
    """"16 people in that department, N of whom you have not contacted."

    N has to be a subset of 16. It was the company-wide count, so the page read
    "16 people in that department, 78 of whom…" — spotted by rendering the page
    rather than by a test.
    """
    repos.crm.update(seeded["ids"][0], status="contacted")   # the data scientist
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    row = openings_frame(repos.conn, settings)[0]

    assert row["same_family_count"] == 1
    assert row["uncontacted_same_family"] == 0, "the only one in-discipline is contacted"
    assert row["uncontacted_count"] == 1, "but someone else at the company is not"
    assert row["uncontacted_same_family"] <= row["same_family_count"]
    assert row["uncontacted_same_family"] <= row["uncontacted_count"]


def test_the_subset_invariant_holds_generally(repos, settings, seeded):
    post(repos, seeded["company"]["id"], "Data Scientist", "https://apex.test/jobs/1")
    post(repos, seeded["company"]["id"], "Race Strategy Engineer", "https://apex.test/jobs/2")
    for row in openings_frame(repos.conn, settings):
        assert row["uncontacted_same_family"] <= row["same_family_count"] <= row["people_count"]
        assert row["uncontacted_same_family"] <= row["uncontacted_count"] <= row["people_count"]
