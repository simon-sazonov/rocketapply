"""Setup's "Reset database" clears the people side only, after a backup.

It used to delete the whole file: every job, rating and application RocketApply
keeps went with the CRM notes, and nothing was copied first.
"""

from __future__ import annotations

import sqlite3

import pytest

from pitwall.db import NETWORKING_RESET_TABLES, ResetRefused, reset_networking
from pitwall.sources.jobs import JobPosting


def _fill(repos) -> dict[str, int]:
    conn = repos.conn
    conn.execute("INSERT INTO candidates (linkedin_slug, profile_url, full_name)"
                 " VALUES ('ada', 'https://linkedin.com/in/ada', 'Ada')")
    cid = conn.execute("SELECT id FROM candidates").fetchone()[0]
    conn.execute("INSERT INTO crm (candidate_id, note) VALUES (?, 'met at Spa')", (cid,))
    conn.execute("INSERT INTO blocklist (linkedin_slug, reason) VALUES ('erased', 'GDPR')")
    people = repos.runs.start({}, "run")
    repos.runs.finish(people)
    jobs_run = repos.runs.start({}, "job-search")
    repos.runs.finish(jobs_run)
    src = repos.job_sources.upsert("t", "job_board")
    job = repos.jobs.upsert(src, JobPosting(title="Race Engineer", url="https://t.test/1")).job_id
    conn.execute("INSERT INTO job_review (cluster_id, feedback) VALUES (?, 'good_match')", (job,))
    conn.execute("INSERT INTO applications (cluster_id, job_id, company_name, title, status)"
                 " VALUES (?, ?, 'Team WRT', 'Race Engineer', 'APPLIED')", (job, job))
    return {"people_run": people, "jobs_run": jobs_run}


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_reset_backs_up_then_clears_only_the_people_side(repos, settings, tmp_path):
    ids = _fill(repos)
    backups = tmp_path / "backups"

    backup, deleted = reset_networking(repos.conn, settings.db_file, backups)

    # the backup is a full, readable copy taken before anything went
    assert backup.parent == backups and backup.exists()
    copy = sqlite3.connect(backup)
    assert _count(copy, "candidates") == 1 and _count(copy, "crm") == 1
    assert _count(copy, "applications") == 1
    copy.close()

    assert deleted["candidates"] == 1 and deleted["crm"] == 1 and deleted["runs"] == 1
    for table in NETWORKING_RESET_TABLES:
        assert _count(repos.conn, table) == 0, table
    # RocketApply's side, the blocklist and the job-search run are untouched
    assert _count(repos.conn, "jobs") == 1
    assert _count(repos.conn, "job_review") == 1
    assert _count(repos.conn, "applications") == 1
    assert _count(repos.conn, "blocklist") == 1
    assert [r["id"] for r in repos.runs.latest(5)] == [ids["jobs_run"]]


def test_reset_defaults_to_data_backups_beside_the_file(repos, settings):
    _fill(repos)
    backup, _ = reset_networking(repos.conn, settings.db_file)
    assert backup.parent == settings.db_file.parent / "backups"


def test_reset_refuses_while_a_pitwall_run_is_going(repos, settings, tmp_path):
    _fill(repos)
    live = repos.runs.start({}, "resume-llm")
    repos.runs.progress(live, "llm", "reading people", 1, 9)

    with pytest.raises(ResetRefused):
        reset_networking(repos.conn, settings.db_file, tmp_path / "backups")
    assert _count(repos.conn, "candidates") == 1
    assert not (tmp_path / "backups").exists()


def test_a_running_job_search_does_not_block_the_reset(repos, settings, tmp_path):
    _fill(repos)
    live = repos.runs.start({}, "job-search")
    repos.runs.progress(live, "fetch", "Adzuna", 1, 9)

    reset_networking(repos.conn, settings.db_file, tmp_path / "backups")
    assert _count(repos.conn, "candidates") == 0
    assert repos.runs.active(commands=("job-search",))["id"] == live
