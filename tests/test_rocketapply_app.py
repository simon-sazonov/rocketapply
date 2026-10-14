"""RocketApply, run for real under Streamlit's AppTest.

Every screen opens on an empty database and on a populated one, and the
process runs end to end without a browser: search → select → documents ready
→ approve → export (a CSV with the files' paths) → in process → mark the batch
applied → applied, with every step in `application_events`. Generation itself
is stubbed (its own tests cover it); what is tested here is that the screens
drive the lifecycle, and that the app never names its owner.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from streamlit.testing.v1 import AppTest

from pitwall.jobsearch import tracker

from .job_app import _populate, job_app  # noqa: F401, TID252
from .paths import ROCKET_APP, on_path  # noqa: TID252

APP = ROCKET_APP / "app.py"
VIEWS = ("search", "selected", "generate", "review", "applications", "engine", "settings")


@pytest.fixture()
def rocket(job_app):  # noqa: F811 - the imported fixture, requested by name
    """The app root, with RocketApply's process-wide caches empty."""
    import streamlit as st

    on_path(ROCKET_APP)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield job_app
    st.cache_resource.clear()
    st.cache_data.clear()


def _app(view: str | None = None, **state: object) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=90)
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    if view:
        at.switch_page(f"views/{view}.py").run()
    return at


def _ok(at: AppTest) -> None:
    assert not at.exception, [repr(e.value) for e in at.exception]


def _conn():
    from pitwall.config import get_settings
    from pitwall.db import get_conn, init_schema

    conn = get_conn(get_settings())
    init_schema(conn)
    return conn


@pytest.mark.parametrize("view", VIEWS)
def test_every_screen_opens_on_an_empty_database(rocket, view):
    _ok(_app(view))


def test_search_lists_the_collected_jobs(rocket):
    _populate()
    at = _app()
    _ok(at)
    assert any(df.value.shape[0] > 0 for df in at.dataframe), "the results table has rows"


def test_the_navigation_carries_each_stage_count():
    from rocket.components import nav_titles

    titles = nav_titles({"found": 10907, "selected": 8, "docs_ready": 41, "in_process": 2,
                         "applied": 1}, generating=(44, 49))
    assert titles == {"search": "Search · 10,907", "selected": "Selected · 8",
                      "generate": "Generate · 44/49", "review": "Review · 41",
                      "applications": "Applications · 3"}
    assert nav_titles({})["review"] == "Review", "no count when nothing waits"


def test_a_status_is_named_after_the_page_it_is_on():
    from rocket.format import STATUS_NAMES

    assert [STATUS_NAMES[s] for s in ("SHORTLISTED", "CV_READY", "IN_PROCESS", "APPLIED")] \
        == ["Selected", "In review", "In process", "Applied"]


def test_select_the_top_n_moves_jobs_to_selected(rocket):
    _populate()
    at = _app(top_n=2)
    next(b for b in at.button if str(b.label).startswith("Select the top")).click().run()
    _ok(at)
    assert len(tracker.by_status(_conn(), ("SHORTLISTED",))) == 2
    at.switch_page("views/selected.py").run()
    _ok(at)
    assert any("Generate" in str(b.label) for b in at.button)


def _generated(tmp: pathlib.Path, conn, n: int = 2) -> list[int]:
    """Stand-in for a generation run: folders on disk, CV_READY in the tracker."""
    from pitwall.jobsearch.store import JobQuery, search_ids

    ids = search_ids(conn, JobQuery(include_filtered=True), limit=n)
    apps = []
    for job_id in ids:
        folder = tmp / "output" / "1_ready_to_send" / f"Job_{job_id}"
        folder.mkdir(parents=True)
        (folder / "provenance.json").write_text(json.dumps({
            "generated_at": "2026-09-29T09:00:00+00:00",
            "job": {"id": job_id, "company": "Apex", "title": "ML", "prompt_version": "v8"},
            "letter": {"state": "clean"}}), encoding="utf-8")
        for suffix in ("_CV.pdf", "_Cover_Letter.txt", "_Cover_Letter.pdf"):
            (folder / f"X_Job_{job_id}{suffix}").write_bytes(b"Dear team,\n\nx")
        apps.append(tracker.record_generation(conn, job_id=job_id,
                                              directory=str(folder.resolve()),
                                              letter_state="clean"))
    return apps


def test_approve_export_and_mark_applied_from_the_screens(rocket):
    _populate()
    conn = _conn()
    apps = _generated(rocket, conn)

    at = _app("review")
    _ok(at)
    next(b for b in at.button if b.label == "Approve").click().run()
    _ok(at)
    approved = [a for a in tracker.by_status(conn, ("CV_READY",)) if a["reviewed_at"]]
    assert len(approved) == 1

    next(b for b in at.button if str(b.label).startswith("Export")).click().run()
    _ok(at)
    [batch] = tracker.batches(conn)
    assert pathlib.Path(batch["csv_path"]).is_file()
    assert tracker.get(conn, approved[0]["id"])["status"] == "IN_PROCESS"

    at.switch_page("views/applications.py").run()
    _ok(at)
    next(b for b in at.button if str(b.label).startswith("Mark 1 as applied")).click().run()
    _ok(at)
    app = tracker.get(conn, approved[0]["id"])
    assert app["status"] == "APPLIED" and app["date_applied"]
    assert [e["to_status"] for e in tracker.events(conn, app["id"])] == \
        ["SHORTLISTED", "CV_READY", "IN_PROCESS", "APPLIED"]
    other = next(a for a in apps if a != approved[0]["id"])
    assert tracker.get(conn, other)["status"] == "CV_READY", "unapproved stays in Review"

    # The sent application's documents leave the "ready" pile; the other's stay.
    sent = pathlib.Path(app["generation_dir"])
    assert sent.parent.name == "4_already_sent" and (sent / "provenance.json").is_file()
    assert not pathlib.Path(approved[0]["generation_dir"]).exists()
    [item] = tracker.batch_items(conn, int(batch["id"]))
    assert item["exported_dir"] == str(sent)
    assert pathlib.Path(tracker.get(conn, other)["generation_dir"]).is_dir()


def test_the_applied_screen_offers_the_people_panel(rocket):
    _populate()
    conn = _conn()
    [app_id] = _generated(rocket, conn, n=1)
    tracker.set_status(conn, app_id, "APPLIED")
    at = _app("applications", app_tabs="Applied")
    _ok(at)
    text = " ".join(str(m.value) for m in at.markdown)
    assert "People at" in text
    assert any("Find people" in str(b.label) for b in at.button)


def test_the_generate_screen_draws_a_finished_run_from_disk(rocket, monkeypatch):
    """After a restart the registry rebuilds the last run from its events file."""
    from pitwall.apply.events import StageEvent

    _populate()
    conn = _conn()
    job_id = int(conn.execute("SELECT id FROM jobs LIMIT 1").fetchone()[0])
    folder = rocket / "run" / "rocketapply" / "20260929-100000"
    folder.mkdir(parents=True)
    events = [
        StageEvent(job_id, "job", "start"),
        StageEvent(job_id, "analyse", "done", data={"required": [
            {"term": "PyTorch", "quote": "experience with PyTorch"}]}),
        StageEvent(job_id, "match", "done", data={"coverage_required": 0.8,
                                                  "coverage_preferred": 0.5}),
        StageEvent(job_id, "job", "done", data={"status": "ready", "seconds": 12.0}),
    ]
    (folder / "events.jsonl").write_text(
        "\n".join(json.dumps(e.as_dict()) for e in events), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({
        "run_id": "20260929-100000", "job_ids": [job_id], "mode": "cv_letter",
        "status": "running", "started": 0}), encoding="utf-8")
    at = _app("generate")
    _ok(at)
    assert any("Resume" in str(b.label) for b in at.button), "a run that never ended resumes"
    labels = " ".join(e.label for e in at.expander)
    assert "ready to send" in labels


def test_the_app_never_names_its_owner():
    for path in ROCKET_APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Simon" not in text and "Sazonov" not in text, path
