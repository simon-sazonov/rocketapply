"""Generation in a process of its own.

The page follows the run folder; a server that starts while the worker is
still going picks the run up instead of offering to resume it (which would
generate everything twice); a worker that died shows as interrupted; and a
dismissed job is never handed to a new run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from pitwall.apply.events import StageEvent
from pitwall.jobsearch import store, tracker

from .job_app import _populate, job_app  # noqa: F401, TID252
from .test_rocketapply_app import _app, _conn, _ok, rocket  # noqa: F401, TID252


def _job_id() -> int:
    _populate()
    return int(_conn().execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()[0])


def _folder(run_id: str, meta: dict):
    from rocket import runner

    folder = runner.RUN_ROOT / run_id
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"run_id": run_id, **meta}), encoding="utf-8")
    return folder


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_the_worker_reports_through_the_run_folder(rocket, monkeypatch):  # noqa: F811
    from pitwall.apply import batch
    from rocket import worker

    folder = _folder("r1", {"job_ids": [7], "mode": "cv", "status": "running", "started": 5})

    def fake_batch(ids, *, on_event, **kw):
        on_event(StageEvent(7, "job", "start"))
        on_event(StageEvent(7, "job", "done", data={"status": "ready"}))

    monkeypatch.setattr(batch, "run_batch", fake_batch)
    assert worker.main("r1") == 0
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["status"] == "done" and meta["pid"] == os.getpid()
    assert meta["started"] == 5, "a resumed run keeps its start"
    assert len((folder / "events.jsonl").read_text().splitlines()) == 2


def test_a_run_whose_worker_is_still_going_is_followed_not_resumed(rocket):  # noqa: F811
    from rocket import runner

    job_id = _job_id()
    folder = _folder("r2", {"job_ids": [job_id], "mode": "cv", "status": "running",
                            "started": 0, "pid": os.getpid()})
    (folder / "events.jsonl").write_text(
        json.dumps(StageEvent(job_id, "job", "start").as_dict()) + "\n", encoding="utf-8")
    at = _app("generate")
    _ok(at)
    assert not any("Resume" in str(b.label) for b in at.button)

    run = runner.registry().generation
    assert run.status == "running" and run.alive
    with (folder / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(StageEvent(job_id, "job", "done",
                                       data={"status": "ready"}).as_dict()) + "\n")
        fh.write('{"job_id": ')                       # a line still being written
    runner.sync(run)
    assert run.jobs[job_id].status == "ready"


def test_a_worker_that_died_leaves_the_run_interrupted(rocket):  # noqa: F811
    from rocket import runner

    job_id = _job_id()
    _folder("r3", {"job_ids": [job_id], "mode": "cv", "status": "running", "started": 0,
                   "pid": _dead_pid()})
    run = runner.registry().generation
    assert run.status == "interrupted"

    run = runner.GenerationRun(run_id="r3", job_ids=[job_id], mode="cv", pid=_dead_pid())
    runner.sync(run)
    assert run.status == "interrupted" and run.error


def test_a_generation_runs_detached_and_the_page_sees_it_finish(rocket, tmp_path,  # noqa: F811
                                                                monkeypatch):
    from rocket import runner

    job_id = _job_id()
    fake = tmp_path / "fake_worker.py"
    fake.write_text(
        "import json, os, pathlib, sys, time\n"
        "folder = pathlib.Path('run/rocketapply') / sys.argv[1]\n"
        "meta = json.loads((folder / 'meta.json').read_text())\n"
        "event = {'job_id': meta['job_ids'][0], 'stage': 'job', 'status': 'done',\n"
        "         'data': {'status': 'ready'}}\n"
        "with (folder / 'events.jsonl').open('a') as fh:\n"
        "    fh.write(json.dumps(event) + '\\n')\n"
        "meta.update(status='done', pid=os.getpid(), finished=time.time())\n"
        "(folder / 'meta.json').write_text(json.dumps(meta))\n"
        "print('own session', os.getsid(0) == os.getpid())\n",
        encoding="utf-8")
    monkeypatch.setattr(runner, "WORKER", fake)

    run = runner.start_generation([job_id], mode="cv")
    assert run is not None and run.pid
    run.proc.wait(timeout=30)
    assert not runner.generation_active()
    assert run.status == "done"
    assert run.jobs[job_id].status == "ready"
    assert "own session True" in (run.folder / "worker.log").read_text()


def test_a_dismissed_job_is_not_handed_to_a_new_run(rocket, monkeypatch):  # noqa: F811
    from rocket import runner

    job_id = _job_id()
    conn = _conn()
    store.dismiss(conn, int(tracker.cluster_for_job(conn, job_id)["cluster_id"]), "Closed")
    conn.commit()
    monkeypatch.setattr(runner.subprocess, "Popen", None)   # must not get that far
    assert runner.start_generation([job_id], mode="cv") is None
