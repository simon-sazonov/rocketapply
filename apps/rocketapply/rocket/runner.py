"""Work that outlives a page view: fetching jobs, generating, finding people.

One registry per server process (`st.cache_resource`), not per browser tab:
a refresh opens a new session, and a thread handle kept in `session_state`
would be lost with it — inviting a second generation that spends the same
Groq quota twice. The registry allows one generation at a time, full stop.

Worker threads never touch `st.*`. They write to their run object (under its
lock).

A generation runs for an hour, longer than a Streamlit server can be counted on
to live: a host that stops idle servers (the Claude app's preview stops one
after 30 idle minutes), a closed terminal, a restart. So it runs in its own
process (`worker.py`, in a session of its own), which appends every event to
`run/rocketapply/<run_id>/events.jsonl` with a heartbeat and its pid in
`meta.json`. The page follows those files (`sync`), and a server that starts
while the worker is still going picks the run up where the files say it is. If
the worker itself dies, the run shows as interrupted, and Resume continues it —
the batch ledger skips every job that already finished.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import streamlit as st

from pitwall.apply.events import StageEvent
from pitwall.apply.pipeline import STAGES

from . import data

RUN_ROOT = pathlib.Path("run") / "rocketapply"
STAGE_KEYS = [k for k, _ in STAGES]
WORKER = pathlib.Path(__file__).resolve().parent / "worker.py"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# ───────────────────────────── fetching jobs ─────────────────────────────


@dataclass
class SearchRun:
    started: float = field(default_factory=time.time)
    after_run_id: int = 0
    thread: threading.Thread | None = None
    error: str = ""

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


# ─────────────────────────────── generating ──────────────────────────────


@dataclass
class JobTrack:
    job_id: int
    company: str = ""
    title: str = ""
    score: float | None = None
    #: stage key -> start | done | skip | error
    stages: dict[str, str] = field(default_factory=dict)
    #: stage key -> the `data` of its done event
    out: dict[str, dict[str, Any]] = field(default_factory=dict)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    waits: list[str] = field(default_factory=list)
    status: str = "queued"          # queued | running | ready | review | blocked | error
    seconds: float = 0.0
    tokens: int = 0
    row: dict[str, Any] = field(default_factory=dict)
    current: str = ""


@dataclass
class GenerationRun:
    run_id: str
    job_ids: list[int]
    mode: str
    hints: dict[int, str] = field(default_factory=dict)
    status: str = "running"         # running | done | error | interrupted
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    error: str = ""
    jobs: dict[int, JobTrack] = field(default_factory=dict)
    research: list[str] = field(default_factory=list)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: The worker process, when this server started it (`proc`) or found it
    #: still running (`pid` only), and how far into events.jsonl `sync` has read.
    pid: int = 0
    proc: subprocess.Popen[bytes] | None = None
    offset: int = 0
    workers: int = 3
    fresh: bool = False

    @property
    def folder(self) -> pathlib.Path:
        return RUN_ROOT / self.run_id

    @property
    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.pid:
            return _pid_alive(self.pid)
        return self.thread is not None and self.thread.is_alive()

    @property
    def finished_jobs(self) -> list[JobTrack]:
        return [j for j in self.jobs.values() if j.status not in ("queued", "running")]

    @property
    def tokens(self) -> int:
        return sum(j.tokens for j in self.jobs.values())

    def eta_seconds(self, workers: int) -> float | None:
        done = self.finished_jobs
        if not done:
            return None
        per = sum(j.seconds for j in done) / len(done)
        left = len(self.job_ids) - len(done)
        return per * left / max(1, workers)

    # called from worker threads
    def on_event(self, event: StageEvent) -> None:
        with self.lock:
            self._apply(event)
        self._persist(event)

    def _apply(self, event: StageEvent) -> None:
        if event.stage == "research":
            self.research.append(event.message)
            return
        job = self.jobs.setdefault(event.job_id, JobTrack(job_id=event.job_id))
        if event.stage == "job":
            if event.status == "start":
                job.status = "running"
            else:
                row = dict(event.data)
                job.row = row
                job.status = str(row.get("status") or ("error" if event.status == "error"
                                                        else "ready"))
                job.seconds = float(row.get("seconds") or event.seconds)
                job.tokens = int(event.tokens or 0)
                job.current = ""
            return
        if event.status == "draft":
            job.drafts.append(dict(event.data))
            job.drafts = job.drafts[-4:]
            job.current = event.message
            return
        if event.status == "wait":
            job.waits.append(event.message)
            job.waits = job.waits[-3:]
            job.current = event.message
            return
        job.stages[event.stage] = event.status
        if event.status == "start":
            job.current = event.message
        if event.status == "skip":
            job.out[event.stage] = {"reason": event.message, **dict(event.data)}
        elif event.status == "done" and event.data:
            job.out[event.stage] = dict(event.data)
        if event.status == "error":
            job.out.setdefault(event.stage, {})["error"] = event.message
        job.tokens = max(job.tokens, int(event.tokens or 0))

    def _persist(self, event: StageEvent) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.as_dict(), ensure_ascii=False, default=str)
            with (self.folder / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.write_meta()
        except OSError:
            pass

    def write_meta(self) -> None:
        meta = {"run_id": self.run_id, "job_ids": self.job_ids, "mode": self.mode,
                "hints": {str(k): v for k, v in self.hints.items()}, "status": self.status,
                "started": self.started, "finished": self.finished, "heartbeat": time.time(),
                "error": self.error, "pid": self.pid, "workers": self.workers,
                "fresh": self.fresh}
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _event(line: str) -> StageEvent | None:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    return StageEvent(job_id=int(d.get("job_id") or 0), stage=d.get("stage", ""),
                      status=d.get("status", ""), message=d.get("message", ""),
                      fraction=float(d.get("fraction") or 0), seconds=float(d.get("seconds") or 0),
                      tokens=int(d.get("tokens") or 0), data=d.get("data") or {})


def _replay(run: GenerationRun) -> None:
    """Rebuild a run's tracks from its events file (after a restart)."""
    path = run.folder / "events.jsonl"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        event = _event(line)
        if event is not None:
            run._apply(event)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_meta(folder: pathlib.Path) -> dict[str, Any]:
    try:
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def sync(run: GenerationRun | None) -> None:
    """Bring a worker's run up to date from its folder: the events appended
    since the last look, and how it ended once the process has gone. A run
    from before workers (a thread in this server) has nothing to read."""
    if run is None or not getattr(run, "pid", 0):
        return
    with run.lock:
        try:
            with (run.folder / "events.jsonl").open("rb") as fh:
                fh.seek(run.offset)
                chunk = fh.read()
        except OSError:
            chunk = b""
        end = chunk.rfind(b"\n") + 1            # a half-written last line waits
        for line in chunk[:end].decode("utf-8", "replace").splitlines():
            event = _event(line)
            if event is not None:
                run._apply(event)
        run.offset += end
        if run.status != "running" or run.alive:
            return
        meta = _read_meta(run.folder)
        ended = meta.get("status") in ("done", "error")
        run.status = meta["status"] if ended else "interrupted"
        run.error = meta.get("error", "") if ended else "The worker process stopped."
        run.finished = float(meta.get("finished") or time.time())
    data.bump()


# ─────────────────────────────── people ──────────────────────────────────


@dataclass
class PeopleRun:
    company: str
    started: float = field(default_factory=time.time)
    messages: list[str] = field(default_factory=list)
    status: str = "running"
    error: str = ""
    thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


@dataclass
class OutreachRun:
    """People and emails for many applications at once."""

    total: int
    started: float = field(default_factory=time.time)
    current: int = 0
    messages: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    status: str = "running"
    error: str = ""
    stop: bool = False
    thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


# ────────────────────────────── the registry ─────────────────────────────


@dataclass
class Registry:
    search: SearchRun | None = None
    generation: GenerationRun | None = None
    people: dict[str, PeopleRun] = field(default_factory=dict)
    outreach: OutreachRun | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def registry() -> Registry:
    reg = Registry()
    reg.generation = _latest_from_disk()
    return reg


def _latest_from_disk() -> GenerationRun | None:
    """The most recent generation on disk, marked interrupted if it never ended."""
    if not RUN_ROOT.exists():
        return None
    metas = sorted(RUN_ROOT.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in metas[:1]:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run = GenerationRun(run_id=meta["run_id"], job_ids=[int(j) for j in meta["job_ids"]],
                            mode=meta.get("mode", "cv_letter"),
                            hints={int(k): v for k, v in (meta.get("hints") or {}).items()},
                            status=meta.get("status", "done"), started=meta.get("started", 0),
                            finished=meta.get("finished", 0), error=meta.get("error", ""),
                            workers=int(meta.get("workers") or 3),
                            fresh=bool(meta.get("fresh")))
        _seed_tracks(run)
        pid = int(meta.get("pid") or 0)
        if run.status == "running" and pid and _pid_alive(pid):
            run.pid = pid                # the worker outlived the last server: follow it
            sync(run)
            return run
        _replay(run)
        if run.status == "running":
            run.status = "interrupted"
        return run
    return None


def _seed_tracks(run: GenerationRun) -> None:
    ids = run.job_ids
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = data.conn().execute(
        f"SELECT j.id, j.title, COALESCE(t.name, NULLIF(j.posting_company,''), c.name, '')"
        f" AS company, m.score FROM jobs j LEFT JOIN job_matches m ON m.job_id = j.id"
        f" LEFT JOIN target_companies t ON t.id = j.target_company_id"
        f" LEFT JOIN companies c ON c.id = j.company_id WHERE j.id IN ({marks})", ids,
    ).fetchall()
    info = {int(r["id"]): r for r in rows}
    for job_id in ids:
        r = info.get(job_id)
        run.jobs[job_id] = JobTrack(job_id=job_id, company=r["company"] if r else "",
                                    title=r["title"] if r else "",
                                    score=r["score"] if r else None)


# ─────────────────────────────── starting ────────────────────────────────


def search_active() -> bool:
    """A job-search run anywhere — this app, PitWall, or the 07:00 launchd job.
    Thirty minutes of silence before it counts as dead: one slow source
    (MyCareersFuture took 15 minutes this morning) is not a crash."""
    reg = registry()
    if reg.search is not None and reg.search.alive:
        return True
    from pitwall.db import JOB_SEARCH_COMMANDS, RunRepo

    return RunRepo(data.conn()).active(30.0, commands=JOB_SEARCH_COMMANDS) is not None


def start_search(*, rerank: bool = False) -> bool:
    """Collect new jobs (or, with `rerank`, re-score the stored ones after a
    profile change — no network, a few minutes)."""
    reg = registry()
    with reg.lock:
        if search_active():
            return False
        last = data.last_search_run()
        run = SearchRun(after_run_id=int(last["id"]) if last else 0)

        def worker() -> None:
            try:
                from pitwall.jobsearch.run import JobSearch

                search = JobSearch()
                search.rerank() if rerank else search.run()
            except Exception as exc:  # noqa: BLE001 - also recorded on the run row
                run.error = str(exc)
            finally:
                data.bump()

        run.thread = threading.Thread(target=worker, daemon=True, name="ra-fetch-jobs")
        reg.search = run
        run.thread.start()
    return True


def generation_active() -> bool:
    gen = registry().generation
    sync(gen)
    return gen is not None and gen.alive


def start_generation(job_ids: list[int], *, mode: str, hints: dict[int, str] | None = None,
                     workers: int = 3, resume_of: GenerationRun | None = None,
                     fresh: bool = False) -> GenerationRun | None:
    reg = registry()
    with reg.lock:
        if generation_active():
            return None
        dismissed = data.dismissed_jobs(list(job_ids))
        job_ids = [j for j in job_ids if j not in dismissed]
        if not job_ids:
            return None
        run_id = resume_of.run_id if resume_of else dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run = GenerationRun(run_id=run_id, job_ids=list(job_ids), mode=mode,
                            hints=dict(hints or {}), workers=max(1, workers), fresh=fresh,
                            started=resume_of.started if resume_of else time.time())
        _seed_tracks(run)
        run.write_meta()
        # Reading events.jsonl from the start replays a resumed run's history too.
        with (run.folder / "worker.log").open("ab") as log:
            run.proc = subprocess.Popen(
                [sys.executable, str(WORKER), run_id], cwd=os.getcwd(), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        run.pid = run.proc.pid
        reg.generation = run
    sync(run)
    return run


def start_people(company: str, *, job_title: str = "", domain: str = "",
                 groups: tuple[str, ...] | None = None) -> PeopleRun | None:
    reg = registry()
    key = company.strip().casefold()
    with reg.lock:
        existing = reg.people.get(key)
        if existing is not None and existing.alive:
            return None
        run = PeopleRun(company=company)

        def progress(stage: str, message: str, current: int = 0, total: int = 0) -> None:
            run.messages.append(f"{stage}: {message}" + (f" ({current}/{total})" if total else ""))
            run.messages = run.messages[-8:]

        def worker() -> None:
            from pitwall.jobsearch.people import GROUPS, find_people

            try:
                find_people(company, job_title=job_title, domain=domain,
                            groups=groups or GROUPS, on_progress=progress)
                run.status = "done"
            except Exception as exc:  # noqa: BLE001 - shown in the People panel
                run.status, run.error = "error", str(exc)
            finally:
                data.bump()

        run.thread = threading.Thread(target=worker, daemon=True, name=f"ra-people-{key}")
        reg.people[key] = run
        run.thread.start()
    return run


def people_run(company: str) -> PeopleRun | None:
    return registry().people.get(company.strip().casefold())


def start_outreach(applications: list[dict[str, Any]], *, per_company: int, emails: bool,
                   research: bool) -> OutreachRun | None:
    """One background run over many applications. None if one is already going."""
    from pitwall.apply.outreach import BulkPlan

    reg = registry()
    plan = BulkPlan(applications=applications, per_company=per_company, emails=emails,
                    research=research)
    with reg.lock:
        if reg.outreach is not None and reg.outreach.alive:
            return None
        run = OutreachRun(total=len(plan.companies()))

        def progress(message: str, current: int = 0, total: int = 0) -> None:
            run.current = current or run.current
            run.messages = [*run.messages, message][-6:]

        def worker() -> None:
            from pitwall.apply.outreach import run_bulk

            try:
                run.counts = run_bulk(plan, data.conn(), on_progress=progress,
                                      should_stop=lambda: run.stop)
                run.status = "stopped" if run.stop else "done"
            except Exception as exc:  # noqa: BLE001 - shown in the bulk panel
                run.status, run.error = "error", str(exc)
            finally:
                data.bump()

        run.thread = threading.Thread(target=worker, daemon=True, name="ra-outreach")
        reg.outreach = run
        run.thread.start()
    return run


def outreach_run() -> OutreachRun | None:
    return registry().outreach
