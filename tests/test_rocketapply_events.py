"""Watching a generation: the stage events RocketApply's Generate view draws.

The promise is "see what the model did", not "see that it is busy". So the
events have to arrive in stage order, carry what each stage produced, survive
a listener that throws, and — in a batch — say which job they belong to.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from pitwall.apply import batch as batch_mod
from pitwall.apply.events import STAGE_TASKS, StageEvent
from pitwall.apply.llm import TaskRouter
from pitwall.jobsearch import tracker
from pitwall.jobsearch.store import load_inbox

from .test_apply_pipeline import StubClient  # noqa: TID252
from .test_jobsearch_pipeline import (  # noqa: F401, TID252
    Stub,
    arbeitnow_postings,
    js_project,
    search,
)


@pytest.fixture()
def one_job(search, settings, monkeypatch, tmp_path):  # noqa: F811 - imported fixture
    """A scored posting in a test database the engine's own `get_conn()` reads,
    with `output/` and `run/` in a scratch working directory."""
    from pitwall.config import get_settings

    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    monkeypatch.setenv("DB_PATH", str(settings.db_file))
    get_settings.cache_clear()
    monkeypatch.chdir(tmp_path)
    job = max(load_inbox(search.conn).view("all"), key=lambda c: c["score"] or 0)
    return search.conn, int(job["id"])


def _cv_only(job_id, listener):
    from pitwall.apply.pipeline import generate_application

    return generate_application(job_id, out="output", letter=False, no_llm=True,
                                on_event=listener)


def test_a_cv_only_run_reports_every_stage_in_order(one_job):
    _, job_id = one_job
    seen: list[StageEvent] = []
    result = _cv_only(job_id, seen.append)
    assert not result.error
    assert [(e.stage, e.status) for e in seen] == [
        ("analyse", "start"), ("analyse", "skip"), ("match", "skip"),
        ("truth", "start"), ("truth", "done"),
        ("select", "start"), ("select", "done"),
        ("render", "start"), ("render", "done"),
        ("plan", "skip"), ("write", "skip"),
        ("package", "start"), ("package", "done"),
    ]
    assert all(e.job_id == job_id for e in seen)


def test_each_done_event_carries_what_the_stage_produced(one_job):
    _, job_id = one_job
    seen: list[StageEvent] = []
    result = _cv_only(job_id, seen.append)
    done = {e.stage: e for e in seen if e.status == "done"}
    projects = done["select"].data["projects"]
    assert projects and {"id", "name", "score", "why"} <= set(projects[0])
    assert done["render"].data["pages"] == 1
    assert "forbidden_count" in done["truth"].data
    package = done["package"].data
    assert pathlib.Path(package["cv_pdf"]).is_file(), "the path is where the file IS, after filing"
    assert package["cv_pdf"] == str(result.cv_pdf)
    assert done["select"].seconds >= 0
    for event in seen:          # everything a UI stores must survive JSON
        __import__("json").dumps(event.as_dict())


def test_a_listener_that_raises_cannot_break_the_run(one_job):
    _, job_id = one_job

    def broken(event):
        raise RuntimeError("the screen fell over")

    result = _cv_only(job_id, broken)
    assert not result.error and result.cv_pdf is not None and result.cv_pdf.is_file()


def test_only_three_stages_ever_touch_a_model():
    assert set(STAGE_TASKS) == {"analyse", "plan", "write"}


# ── the batch ──


def test_a_cv_only_batch_reports_each_job_and_records_it(one_job, tmp_path, monkeypatch):
    conn, job_id = one_job
    monkeypatch.setattr(batch_mod, "LEDGER_DIR", tmp_path / "ledger")
    folder = tmp_path / "output" / "1_ready_to_send" / "Apex_ML"
    folder.mkdir(parents=True)
    (folder / "Simon_Sazonov_Apex_ML_CV.pdf").write_bytes(b"%PDF")
    asked: dict = {}

    def fake_generate(jid, **kw):
        asked.update(kw)
        kw["on_event"](StageEvent(job_id=jid, stage="render", status="done"))
        return SimpleNamespace(company="Apex", title="ML", prompt_tokens=10,
                               completion_tokens=5, letter_path=None, cv_pdf=folder / "x.pdf",
                               directory=folder, blocked=False, error="", letter_state="")

    events: list[StageEvent] = []
    rows = batch_mod.run_batch([job_id], name="cv-only", out=tmp_path / "output",
                               letter=False, record=True, on_event=events.append,
                               generate=fake_generate, hints={job_id: "lead with RL"})
    assert asked["letter"] is False and asked["hint"] == "lead with RL"
    assert [(e.stage, e.status) for e in events] == [
        ("job", "start"), ("render", "done"), ("job", "done")]
    assert rows[0].status == "ready" and rows[0].mode == "cv"
    assert rows[0].path == str(folder.resolve())
    app = tracker.for_cluster(conn, int(tracker.cluster_for_job(conn, job_id)["cluster_id"]))
    assert app["status"] == "CV_READY" and app["generation_mode"] == "cv"
    assert app["cv_version"] == "Simon_Sazonov_Apex_ML_CV.pdf"


def test_the_batch_does_not_hand_new_arguments_to_an_old_generate(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_mod, "LEDGER_DIR", tmp_path / "ledger")

    def old_style(job_id, *, out, cv, letter, audit):   # no on_event, no hint
        return SimpleNamespace(company="X", title="T", prompt_tokens=0, completion_tokens=0,
                               letter_path=None, cv_pdf=None, directory=None, blocked=False,
                               error="", letter_state="", letter=None, fact=None, job={},
                               truth=None, analysis=None)

    rows = batch_mod.run_batch([1], name="old", out=tmp_path, cv=False, letter=False,
                               research=False, generate=old_style)
    assert rows[0].status == "ready"


def test_letters_are_found_after_filing_moved_them(tmp_path):
    folder = tmp_path / "2_needs_changes" / "Apex_ML"
    folder.mkdir(parents=True)
    (folder / "a_Cover_Letter.txt").write_text("Dear team,", encoding="utf-8")
    row = batch_mod.BatchRow(job_id=1, directory="Apex_ML", letter="a_Cover_Letter.txt")
    assert batch_mod.letter_texts([row], out=tmp_path) == {"Apex_ML": "Dear team,"}


# ── regenerate ──


def test_regenerate_skips_the_cached_answer_but_refreshes_it(tmp_path):
    client = StubClient({"letter": "first"})
    router = TaskRouter(clients={"groq": client}, cache_dir=tmp_path / "cache")
    router.complete_json("letter_write", "sys", "user")
    calls = len(client.prompts)

    fresh = TaskRouter(clients={"groq": client}, cache_dir=tmp_path / "cache",
                       fresh_tasks=frozenset({"letter_write"}))
    fresh.complete_json("letter_write", "sys", "user")
    assert len(client.prompts) == calls + 1, "a regenerate asks the model again"

    router.complete_json("letter_write", "sys", "user")
    assert len(client.prompts) == calls + 1, "and an ordinary call still uses the cache"
