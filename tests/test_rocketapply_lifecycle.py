"""RocketApply's application lifecycle, and schema v10 that stores it.

Selected → documents ready → in process (exported to a CSV batch) → applied.
Three things matter more than the happy path: nothing is ever moved
backwards (regenerating a CV must not un-apply a job), one vacancy is one
application however it is reached, and a whole batch can be marked applied
in one go with the ones you did not send left behind.
"""

from __future__ import annotations

import sqlite3

import pytest

from pitwall.db import SCHEMA_VERSION, init_schema
from pitwall.jobsearch import tracker
from pitwall.jobsearch.store import JobQuery, dismiss, load_inbox, search_jobs

from .test_jobsearch_pipeline import (  # noqa: F401, F811, TID252
    Stub,
    arbeitnow_postings,
    js_project,
    search,
)


@pytest.fixture()
def jobs(search):  # noqa: F811 - the imported fixture, requested by name
    """The Arbeitnow fixture, searched and scored: `{title: jobs.id}`."""
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    return search.conn, {c["title"]: int(c["id"]) for c in load_inbox(search.conn).view("all")}


def _statuses(conn, app_id):
    return [e["to_status"] for e in tracker.events(conn, app_id)]


# ───────────────────────────── the migration ─────────────────────────────


def test_a_v9_database_gains_the_v10_tables_and_columns(tmp_path):
    path = tmp_path / "v9.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    # Wind it back to v9: drop what v10 adds, then forget the version.
    conn.executescript("""
        DROP TABLE export_batch_items; DROP TABLE export_batches; DROP TABLE outreach_notes;
        DELETE FROM schema_version WHERE version = 10;
    """)
    for column in ("generation_dir", "generation_mode", "generated_at", "letter_state",
                   "reviewed_at", "review_note"):
        conn.execute(f"ALTER TABLE applications DROP COLUMN {column}")
    conn.execute("ALTER TABLE job_matches DROP COLUMN missing_languages")
    conn.execute("INSERT INTO applications (company_name, title) VALUES ('Apex', 'ML')")
    conn.commit()

    assert init_schema(conn) == SCHEMA_VERSION == 10
    assert init_schema(conn) == 10, "re-running the migration is a no-op"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"export_batches", "export_batch_items", "outreach_notes"} <= tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    assert {"generation_dir", "letter_state", "reviewed_at"} <= cols
    assert "missing_languages" in {r[1] for r in conn.execute("PRAGMA table_info(job_matches)")}
    assert conn.execute("SELECT company_name FROM applications").fetchone()[0] == "Apex"


# ───────────────────────────── the lifecycle ─────────────────────────────


def test_selecting_a_job_twice_is_one_application(jobs):
    conn, ids = jobs
    job_id = ids["Graduate Machine Learning Engineer (m/w/d)"]
    first = tracker.ensure_for_job(conn, job_id)
    assert tracker.ensure_for_job(conn, job_id) == first
    assert tracker.select_jobs(conn, [job_id]) == [first]
    app = tracker.get(conn, first)
    assert app["status"] == "SHORTLISTED"
    assert app["company_norm"], "normalised, so §39's same-employer check finds it"


def test_the_inbox_sees_an_application_made_from_a_job_id(jobs):
    conn, ids = jobs
    title = "Graduate Machine Learning Engineer (m/w/d)"
    app_id = tracker.ensure_for_job(conn, ids[title])
    cluster = {c["title"]: c for c in load_inbox(conn).view("all")}[title]
    assert cluster["application"]["id"] == app_id


def test_nothing_moves_backwards(jobs):
    conn, ids = jobs
    app_id = tracker.ensure_for_job(conn, next(iter(ids.values())), "APPLIED")
    assert tracker.advance(conn, app_id, "CV_READY") is False
    tracker.record_generation(conn, job_id=tracker.get(conn, app_id)["job_id"],
                              directory="output/1_ready_to_send/X", mode="cv")
    app = tracker.get(conn, app_id)
    assert app["status"] == "APPLIED", "regenerating the CV must not un-apply the job"
    assert app["generation_dir"] == "output/1_ready_to_send/X"


def test_a_skipped_job_comes_back_when_selected_again(jobs):
    conn, ids = jobs
    job_id = next(iter(ids.values()))
    app_id = tracker.ensure_for_job(conn, job_id)
    tracker.skip(conn, app_id)
    assert tracker.get(conn, app_id)["status"] == "SKIPPED"
    assert tracker.select_jobs(conn, [job_id]) == [app_id]
    assert _statuses(conn, app_id) == ["SHORTLISTED", "SKIPPED", "SHORTLISTED"]


def _cluster(conn, job_id):
    return int(tracker.cluster_for_job(conn, job_id)["cluster_id"])


def test_dismissing_a_job_takes_it_out_of_every_stage(jobs):
    conn, ids = jobs
    job_id = next(iter(ids.values()))
    app_id = tracker.record_generation(conn, job_id=job_id, directory="output/x", mode="cv")
    assert tracker.get(conn, app_id)["status"] == "CV_READY"
    before = search_jobs(conn, JobQuery(), limit=100)
    assert job_id in {r["id"] for r in before.rows}

    dismiss(conn, _cluster(conn, job_id), "Closed")

    assert tracker.get(conn, app_id)["status"] == "SKIPPED"
    assert not tracker.by_status(conn, ("SHORTLISTED", "CV_READY", "IN_PROCESS"))
    after = search_jobs(conn, JobQuery(), limit=100)
    assert job_id not in {r["id"] for r in after.rows}
    assert after.total == before.total - 1


def test_a_dismissed_job_stays_out_when_its_documents_finish(jobs):
    """A generation that was already running must not bring it back."""
    conn, ids = jobs
    job_id = next(iter(ids.values()))
    app_id = tracker.select_jobs(conn, [job_id])[0]
    dismiss(conn, _cluster(conn, job_id), "Not interested")
    assert tracker.record_generation(conn, job_id=job_id, directory="output/y",
                                     mode="cv") == app_id
    app = tracker.get(conn, app_id)
    assert app["status"] == "SKIPPED"
    assert app["generation_dir"] == "output/y", "the files are still remembered"


def test_an_application_that_went_out_is_not_touched_by_a_dismissal(jobs):
    conn, ids = jobs
    job_id = next(iter(ids.values()))
    app_id = tracker.ensure_for_job(conn, job_id, "APPLIED")
    dismiss(conn, _cluster(conn, job_id), "Closed")
    assert tracker.get(conn, app_id)["status"] == "APPLIED"


def test_selecting_a_dismissed_job_again_undoes_the_dismissal(jobs):
    conn, ids = jobs
    job_id = next(iter(ids.values()))
    dismiss(conn, _cluster(conn, job_id), "Poor match")
    app_id = tracker.select_jobs(conn, [job_id])[0]
    assert tracker.get(conn, app_id)["status"] == "SHORTLISTED"
    assert not tracker.is_dismissed(conn, job_id)
    assert job_id in {r["id"] for r in search_jobs(conn, JobQuery(), limit=100).rows}


def test_generation_moves_to_documents_ready_and_clears_an_old_review(jobs):
    conn, ids = jobs
    job_id = next(iter(ids.values()))
    app_id = tracker.record_generation(
        conn, job_id=job_id, directory="output/2_needs_changes/A", cv_version="a_CV.pdf",
        letter_version="a_Cover_Letter_NEEDS_REVIEW.txt", letter_state="review",
    )
    tracker.set_review(conn, app_id, approved=True)
    assert tracker.get(conn, app_id)["reviewed_at"]
    tracker.record_generation(conn, job_id=job_id, directory="output/1_ready_to_send/A",
                              letter_state="clean")
    app = tracker.get(conn, app_id)
    assert app["status"] == "CV_READY"
    assert app["reviewed_at"] is None, "a new generation needs a new look"
    assert app["letter_state"] == "clean"
    assert app["cv_version"] == "a_CV.pdf", "an unnamed file keeps the previous name"


def test_export_then_mark_the_batch_applied_leaving_one_behind(jobs):
    conn, ids = jobs
    apps = [tracker.record_generation(conn, job_id=j, directory=f"output/1_ready_to_send/{j}")
            for j in list(ids.values())[:3]]
    batch_id = tracker.create_export_batch(conn, apps, name="2026-09-29 batch 1")
    assert {tracker.get(conn, a)["status"] for a in apps} == {"IN_PROCESS"}
    items = tracker.batch_items(conn, batch_id)
    assert [i["position"] for i in items] == [1, 2, 3]
    assert items[0]["exported_dir"] == f"output/1_ready_to_send/{tracker.get(conn, apps[0])['job_id']}"

    moved = tracker.mark_batch_applied(conn, batch_id, exclude=[apps[2]])
    assert moved == 2
    assert [tracker.get(conn, a)["status"] for a in apps] == ["APPLIED", "APPLIED", "IN_PROCESS"]
    assert tracker.get(conn, apps[0])["date_applied"]
    assert _statuses(conn, apps[0]) == ["SHORTLISTED", "CV_READY", "IN_PROCESS", "APPLIED"]
    [summary] = tracker.batches(conn)
    assert (summary["size"], summary["applied"], summary["in_process"]) == (3, 2, 1)
    assert summary["applied_at"]
    assert tracker.batch_of(conn, apps[0])["id"] == batch_id
    assert f"output/1_ready_to_send/{tracker.get(conn, apps[0])['job_id']}" in tracker.exported_dirs(conn)


def test_a_batch_needs_a_name_and_at_least_one_application(jobs):
    conn, _ = jobs
    with pytest.raises(ValueError):
        tracker.create_export_batch(conn, [], name="empty")
    with pytest.raises(ValueError):
        tracker.create_export_batch(conn, [1], name="  ")


def test_by_status_ranks_by_score(jobs):
    conn, ids = jobs
    for job_id in ids.values():
        tracker.ensure_for_job(conn, job_id)
    rows = tracker.by_status(conn, ("SHORTLISTED",))
    scores = [r["score"] or 0 for r in rows]
    assert scores == sorted(scores, reverse=True) and len(rows) == len(ids)


def test_the_cli_record_path_goes_through_the_tracker(jobs):
    from pitwall.apply.packager import record_application

    conn, ids = jobs
    job_id = next(iter(ids.values()))
    row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    first = record_application(conn, job=row, company="x", cv_version="cv.pdf",
                               letter_version="", directory=__import__("pathlib").Path("o/A"))
    again = record_application(conn, job=row, company="x", cv_version="cv2.pdf",
                               letter_version="", directory=__import__("pathlib").Path("o/A"))
    assert first == again, "generating twice used to insert a second row"
    assert tracker.get(conn, first)["status"] == "CV_READY"


def test_already_applied_matches_the_normalised_employer(jobs):
    from pitwall.apply.jobs import existing_application

    conn, _ = jobs
    tracker.create_manual(conn, company="Anthropic, Inc.", title="Research Engineer")
    assert [a["title"] for a in existing_application("anthropic", conn)] == ["Research Engineer"]
