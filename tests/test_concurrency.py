"""Concurrent stages and serialised writes — §1.1.

The pipeline was strictly sequential; against a cloud API that is the dominant
remaining cost. `ThreadPoolExecutor`, not asyncio: the codebase is synchronous
(`requests`, `sqlite3`) and an asyncio rewrite would touch every module.

The failure this file exists to prevent is `database is locked`. WAL mode makes
readers concurrent with a writer, not writers with each other, and the symptom
only ever appears under load — which is exactly when nobody wants to find it.
Two defences, both tested here:

1. **structural** — workers do network and disk-cache work and return their
   results; the thread that submitted them does every SQLite write;
2. **a process-wide write lock**, because the Streamlit UI thread edits CRM rows
   while a background run is writing, and those really are different threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from pitwall.config import Settings
from pitwall.db import writing
from pitwall.inputs import sync_to_db
from pitwall.pipeline import Pipeline, RunParams, RunStats
from pitwall.ratelimit import AdaptiveConcurrency, RateLimiter, reset_limiters
from pitwall.search.base import RateLimited, SearchHit

from .test_llm_contract import GOOD, FakeClient  # noqa: TID252
from .test_scoring_pipeline import seed_pending  # noqa: TID252

# ─────────────────────────── adaptive concurrency ────────────────────────


def test_the_ceiling_only_ever_goes_down():
    a = AdaptiveConcurrency(8, name="t")
    assert [a.reduce(), a.reduce(), a.reduce(), a.reduce()] == [4, 2, 1, 1]
    assert a.limit == 1 and a.initial == 8


def test_the_ceiling_never_reaches_zero():
    assert AdaptiveConcurrency(1, name="t").reduce() == 1


def test_slots_are_bounded_by_the_current_limit():
    a = AdaptiveConcurrency(2, name="t")
    peak = 0
    live = 0
    lock = threading.Lock()

    def work():
        nonlocal peak, live
        with a.slot():
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
            with lock:
                live -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 2


# ───────────────────── the limiter is shared, not per-thread ─────────────


def test_one_bucket_serves_every_worker():
    """A per-thread limiter is not a rate limit, it is N of them."""
    reset_limiters()
    limiter = RateLimiter(per_second=50, jitter=0.0)
    stamps: list[float] = []
    lock = threading.Lock()

    def work():
        limiter.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= 0.015 for g in gaps), f"workers bypassed the bucket: {gaps}"


# ─────────────────────────── the write lock ──────────────────────────────


def test_the_write_lock_is_re_entrant():
    """A repository write can call another one."""
    with writing(), writing():
        pass


def test_the_write_lock_serialises_two_threads():
    order: list[str] = []

    def hold(tag: str, delay: float):
        with writing():
            order.append(f"{tag}-in")
            time.sleep(delay)
            order.append(f"{tag}-out")

    a = threading.Thread(target=hold, args=("a", 0.05))
    b = threading.Thread(target=hold, args=("b", 0.0))
    a.start()
    time.sleep(0.01)
    b.start()
    a.join()
    b.join()
    # never interleaved: each in is followed by its own out
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])


# ──────────────────────── the concurrent search stage ────────────────────


class SlowProvider:
    """Records which thread each query ran on, and how many were concurrent."""

    name = "ddgs"

    def __init__(self, delay: float = 0.005, fail_after: int | None = None) -> None:
        self.delay = delay
        self.fail_after = fail_after
        self.calls = 0
        self.threads: set[str] = set()
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def supports_operators(self) -> bool:
        return False

    def remaining_quota(self) -> int | None:
        return None

    def search(self, query, *, max_results=20, region="wt-wt"):
        with self._lock:
            self.calls += 1
            n = self.calls
            self._live += 1
            self.peak = max(self.peak, self._live)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(self.delay)
            if self.fail_after is not None and n > self.fail_after:
                raise RateLimited("429 from the upstream engine")
            slug = f"person-{n}"
            return [
                SearchHit(
                    0, f"https://www.linkedin.com/in/{slug}",
                    f"Person {n} - Data Scientist - McLaren Racing | LinkedIn",
                    "Data Scientist at McLaren Racing · Woking, England", {},
                )
            ]
        finally:
            with self._lock:
                self._live -= 1


@pytest.fixture()
def big_pipeline(settings, repos, monkeypatch, company, role):
    """A pipeline with 200+ planned queries and an instant provider."""
    sync_to_db(repos.conn, [company], [role])
    provider = SlowProvider()
    monkeypatch.setattr("pitwall.pipeline.build_chain", lambda *a, **k: [provider])
    reset_limiters()
    settings.ddgs_queries_per_second = 10_000.0     # the bucket is tested above
    settings.max_queries_per_run = 400
    pipe = Pipeline(settings, repos)
    pipe.stub = provider
    return pipe


def _plan(pipe, n: int) -> int:
    """Insert `n` synthetic pending queries directly."""
    from pitwall.models import QuerySpec, make_query_hash

    specs = []
    company = pipe.repos.companies.all()[0]
    role = pipe.repos.roles.all()[0]
    for i in range(n):
        text = f"site:linkedin.com/in McLaren Racing Data Scientist {i}"
        specs.append(
            QuerySpec(
                company_id=company["id"], role_id=role["id"],
                company_name=company["name"], role_title=role["title"],
                template_id="plain_site_soft", query_text=text,
                query_hash=make_query_hash("ddgs", text), priority=1.0, provider="ddgs",
            )
        )
    inserted, _ = pipe.repos.queries.insert_many(specs)
    return inserted


def test_two_hundred_concurrent_queries_never_lock_the_database(big_pipeline, repos):
    """§1.1 acceptance criterion, and the whole reason for the design."""
    assert _plan(big_pipeline, 220) == 220
    stats = RunStats()
    try:
        big_pipeline.search(RunParams(limit=400, concurrency=8), stats)
    except sqlite3.OperationalError as exc:  # pragma: no cover - the failure mode
        pytest.fail(f"concurrent writes collided: {exc}")

    assert stats.queries_executed == 220
    assert stats.queries_failed == 0
    assert repos.results.count() == 220
    assert big_pipeline.stub.peak > 1, "the pool was not actually concurrent"
    assert len(big_pipeline.stub.threads) > 1


def test_the_worker_threads_never_touch_sqlite(big_pipeline, repos):
    """A sqlite3 connection belongs to the thread that opened it.

    Structural, not incidental: this is what makes `database is locked`
    impossible rather than merely unlikely. `set_trace_callback` fires for every
    statement on this connection, so any worker that reached for the database
    would be named here.
    """
    main = threading.current_thread().name
    offenders: list[str] = []

    def trace(statement: str) -> None:
        name = threading.current_thread().name
        if name != main:
            offenders.append(f"{name}: {statement[:60]}")

    _plan(big_pipeline, 40)
    repos.conn.set_trace_callback(trace)
    try:
        stats = RunStats()
        big_pipeline.search(RunParams(limit=100, concurrency=6), stats)
    finally:
        repos.conn.set_trace_callback(None)

    assert offenders == [], f"SQLite was used off the submitting thread: {offenders[:3]}"
    assert stats.queries_executed == 40
    assert big_pipeline.stub.peak > 1, "the run has to be concurrent for this to mean anything"


def test_a_429_permanently_lowers_the_concurrency_for_the_run(big_pipeline, repos):
    big_pipeline.stub.fail_after = 5
    _plan(big_pipeline, 40)
    stats = RunStats()
    big_pipeline.search(RunParams(limit=100, concurrency=8), stats)
    assert any("concurrency reduced" in n for n in stats.notes)
    assert stats.queries_failed > 0     # the rate-limited ones are recorded as failed
    assert stats.queries_executed == 5  # and the ones before it are kept


def test_concurrency_is_configurable_and_conservative_by_default():
    s = Settings(_env_file=None)
    assert s.search_concurrency == 4
    assert s.llm_concurrency == 8


def test_a_run_param_overrides_the_configured_pool(big_pipeline):
    _plan(big_pipeline, 12)
    big_pipeline.search(RunParams(limit=50, concurrency=1), RunStats())
    assert big_pipeline.stub.peak == 1


# ────────────────────────── the concurrent LLM stage ─────────────────────


class CountingClient(FakeClient):
    """A FakeClient that records concurrency and answers every candidate."""

    def __init__(self, delay: float = 0.01) -> None:
        super().__init__([])
        self.delay = delay
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def _create(self, **kwargs):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
            self.calls.append(kwargs)
        try:
            time.sleep(self.delay)
            ids = [
                int(line.split()[1].rstrip(":"))
                for line in kwargs["messages"][1]["content"].splitlines()
                if line.strip().startswith("candidate_id")
            ]
            return _reply(ids or [1])
        finally:
            with self._lock:
                self._live -= 1


def _reply(ids):
    from types import SimpleNamespace

    body = json.dumps(
        {"results": [{**GOOD["results"][0], "candidate_id": i} for i in ids]}
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=body))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )


@pytest.fixture()
def llm_pipeline(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    settings.zai_api_key = "sk-test"
    settings.llm_batch_size = 2
    return Pipeline(settings, repos)


def test_the_llm_stage_runs_batches_in_parallel(llm_pipeline, repos, monkeypatch):
    from pitwall.scoring.llm import LLMScorer

    client = CountingClient()
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: LLMScorer(settings, provider="zai", client=client),
    )
    seed_pending(llm_pipeline, repos, 12)
    stats = llm_pipeline.score_llm(RunParams(use_llm=True, concurrency=4))

    assert stats.llm_calls == 6          # 12 candidates / batch of 2
    assert stats.llm_scored == 12
    assert client.peak > 1, "the batches ran one at a time"


def test_no_candidate_is_scored_twice_under_concurrency(llm_pipeline, repos, monkeypatch):
    from pitwall.scoring.llm import LLMScorer

    client = CountingClient()
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: LLMScorer(settings, provider="zai", client=client),
    )
    ids = seed_pending(llm_pipeline, repos, 10)
    llm_pipeline.score_llm(RunParams(use_llm=True, concurrency=4))

    rows = repos.conn.execute(
        "SELECT candidate_id, COUNT(*) n FROM scores GROUP BY candidate_id"
    ).fetchall()
    assert {r["candidate_id"] for r in rows} == set(ids)
    assert all(r["n"] == 1 for r in rows)


def test_token_accounting_survives_parallel_workers(llm_pipeline, repos, monkeypatch):
    """`+=` on three counters from N threads is a read and a write, not one."""
    from pitwall.scoring.llm import LLMScorer

    client = CountingClient(delay=0.0)
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: LLMScorer(settings, provider="zai", client=client),
    )
    seed_pending(llm_pipeline, repos, 16)
    stats = llm_pipeline.score_llm(RunParams(use_llm=True, concurrency=8))
    assert stats.llm_calls == 8
    assert stats.llm_tokens_in == 8 * 100
    assert stats.llm_tokens_out == 8 * 20
