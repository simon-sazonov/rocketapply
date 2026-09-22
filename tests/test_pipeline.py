"""End-to-end pipeline behaviour — the automatable half of the §12.3 QA checklist.

    - re-running the same search adds 0 duplicate candidates
    - killing a run mid-way and restarting resumes without redoing work
    - every score can be traced to a visible SERP snippet
    - an ignored person stays out of the default view
    - delete & blocklist survives a fresh full run
    - the app works offline with --cache-only

A stub provider stands in for the network, so this file never makes a request.
"""

from __future__ import annotations

import json

import pytest

from pitwall.crm import CRM
from pitwall.export import filter_frame, funnel_counts, leads_frame
from pitwall.inputs import sync_to_db
from pitwall.models import Company, Role
from pitwall.pipeline import Pipeline, RunParams, RunStats

from .conftest import StubProvider  # noqa: TID252


def _params(**kw):
    base = {"max_queries": 4, "max_results": 10, "use_llm": False}
    base.update(kw)
    return RunParams(**base)


# ─────────────────────────────── the stages ──────────────────────────────


def test_a_full_run_produces_traceable_scores(pipeline, repos, settings):
    stats = pipeline.run(_params())
    assert stats.queries_executed == 4
    assert stats.candidates_new == 3          # 5 hits: 1 post dropped, 1 duplicate merged
    assert repos.candidates.count() == 3

    df = leads_frame(repos.conn, settings)
    assert len(df) == 3
    for _, row in df.iterrows():
        evidence = repos.candidates.evidence(int(row["candidate_id"]))
        assert evidence, f"{row['name']} has no evidence"
        assert any(e["title"] or e["snippet"] for e in evidence)


def test_the_health_care_trap_is_rejected_end_to_end(pipeline, repos, settings):
    pipeline.run(_params())
    df = leads_frame(repos.conn, settings)
    brooks = df[df["name"] == "David Brooks"].iloc[0]
    rossi = df[df["name"] == "Luca Rossi"].iloc[0]
    jane = df[df["name"] == "Jane Doe"].iloc[0]
    assert brooks["verdict"] == "reject"
    assert rossi["verdict"] == "reject"
    assert jane["verdict"] == "pending_llm"
    assert jane["heuristic_score"] > brooks["heuristic_score"]


def test_rerunning_adds_no_duplicate_candidates(pipeline, repos):
    pipeline.run(_params())
    first = repos.candidates.count()
    pipeline.run(_params())
    assert repos.candidates.count() == first


def test_the_second_run_spends_no_network_calls(pipeline):
    pipeline.run(_params())
    calls_after_first = pipeline.stub.calls
    pipeline.run(_params())
    assert pipeline.stub.calls == calls_after_first  # freshness skip + SERP cache


def test_mirror_hosts_merge_into_one_person_keeping_the_longest_fields(pipeline, repos):
    pipeline.run(_params())
    jane = repos.candidates.get_by_slug("jane-doe")
    assert jane is not None
    assert jane["current_title"] == "Senior Data Scientist"      # longest wins
    assert "tyre degradation" in (jane["headline"] or "")        # longest headline wins
    assert jane["location"] == "Woking, England, United Kingdom"
    assert repos.candidates.get_by_slug("Jane-Doe") is None      # not a second row
    assert jane["evidence_count"] >= 2                           # several queries found her


def test_an_interrupted_run_resumes_without_redoing_finished_queries(pipeline, repos):
    pipeline.build_queries(_params())
    pipeline.search(_params(limit=2))
    done_after_partial = repos.queries.counts_by_status().get("done", 0)
    assert done_after_partial == 2

    repos.queries.mark(999999, "running")          # a no-op id: nothing to reset
    pipeline.search(_params(limit=4))
    counts = repos.queries.counts_by_status()
    assert counts.get("done", 0) == 4
    assert pipeline.stub.calls == 4                # the first two were not refetched


def test_cache_only_mode_never_calls_the_provider(pipeline, repos):
    pipeline.build_queries(_params())
    stats = pipeline.search(_params(cache_only=True))
    assert pipeline.stub.calls == 0
    assert stats.results_fetched == 0


def test_extract_is_idempotent_unless_reprocess_is_asked_for(pipeline, repos):
    pipeline.run(_params())
    again = pipeline.extract(_params())
    assert again.rows_parsed == 0                  # everything already processed
    reprocessed = pipeline.extract(_params(reprocess=True))
    assert reprocessed.rows_parsed > 0
    assert reprocessed.candidates_new == 0


# ─────────────────────────────── CRM effects ─────────────────────────────


def test_an_ignored_person_disappears_from_the_default_view(pipeline, repos, settings):
    pipeline.run(_params())
    df = leads_frame(repos.conn, settings)
    jane_id = int(df[df["name"] == "Jane Doe"].iloc[0]["candidate_id"])
    CRM(repos.conn).ignore(jane_id)

    refreshed = leads_frame(repos.conn, settings)
    visible = filter_frame(refreshed, hide_ignored=True)
    assert "Jane Doe" not in set(visible["name"])
    assert "Jane Doe" in set(filter_frame(refreshed, hide_ignored=False)["name"])


def test_delete_and_blocklist_survives_a_fresh_full_run(pipeline, repos):
    pipeline.run(_params())
    jane = repos.candidates.get_by_slug("jane-doe")
    CRM(repos.conn).delete_and_block(jane["id"], "not interested")
    assert repos.candidates.get_by_slug("jane-doe") is None

    # a completely fresh sweep: new queries, cache bypassed, everything reprocessed
    pipeline.run(_params(max_queries=6, no_cache=True, reprocess=True))
    assert repos.candidates.get_by_slug("jane-doe") is None
    assert repos.candidates.is_blocked("jane-doe")


# ─────────────────────────── bookkeeping and preview ─────────────────────


def test_the_run_row_records_what_happened(pipeline, repos):
    stats = pipeline.run(_params())
    run = repos.runs.get(stats.run_id)
    assert run["status"] == "done"
    assert run["finished_at"]
    assert run["queries_executed"] == stats.queries_executed
    assert run["results_fetched"] == stats.results_fetched
    assert json.loads(run["params"])["use_llm"] is False


def test_a_cancelled_run_stops_and_is_recorded(pipeline, repos, monkeypatch):
    monkeypatch.setattr(Pipeline, "cancelled", lambda self: True)
    stats = pipeline.run(_params())
    run = repos.runs.get(stats.run_id)
    assert run["status"] == "cancelled"


def test_the_preview_reports_a_plan_without_touching_the_network(pipeline):
    info = pipeline.preview(_params(max_queries=10))
    assert info["queries_after_cap"] <= 10
    assert info["est_serp_rows"] > 0
    assert info["provider"] == "ddgs"
    assert pipeline.stub.calls == 0


def test_the_funnel_counts_line_up_with_the_tables(pipeline, repos, settings):
    pipeline.run(_params())
    funnel = funnel_counts(repos.conn, settings)
    assert funnel["queries"] == 4
    assert funnel["serp_rows"] == repos.results.count()
    assert funnel["candidates"] == repos.candidates.count()
    assert funnel["passed_heuristics"] <= funnel["candidates"]


def test_narrowing_a_run_to_one_company_and_role(settings, repos, monkeypatch, write_inputs):
    companies = [
        Company(name="McLaren Racing", aliases="McLaren F1", negative_keywords="Health Care", tier=1),
        Company(name="AF Corse", aliases="", negative_keywords="", tier=2),
    ]
    roles = [
        Role(title="Data Scientist", family="data_science", aliases="", seniority="", keywords=""),
        Role(title="Race Engineer", family="operations", aliases="", seniority="", keywords=""),
    ]
    write_inputs(companies, roles)
    sync_to_db(repos.conn, companies, roles)
    provider = StubProvider()
    monkeypatch.setattr("pitwall.pipeline.build_chain", lambda *a, **k: [provider])
    pipe = Pipeline(settings, repos)

    pipe.build_queries(RunParams(companies=["AF Corse"], roles=["Race Engineer"], max_queries=50))
    texts = [r["query_text"] for r in repos.queries.pending()]
    assert texts
    assert all("AF Corse" in t and "Race Engineer" in t for t in texts)


def test_llm_stage_is_skipped_cleanly_when_disabled(pipeline, repos):
    stats = pipeline.run(_params(use_llm=False))
    assert stats.llm_calls == 0
    assert repos.quota.used_today("llm:zai") == 0


# ───────────────── the background-worker contract (§10) ──────────────────


def test_a_run_in_a_worker_thread_streams_progress_to_the_main_thread(
    settings, repos, monkeypatch, write_inputs, company, role
):
    """What the 🔎 Run Search page depends on.

    The worker must never touch Streamlit state: it opens its own thread-local
    SQLite connection, creates the run row, and streams progress through
    `run_progress`, which the UI thread polls.
    """
    import threading

    write_inputs([company], [role])
    sync_to_db(repos.conn, [company], [role])
    provider = StubProvider()
    monkeypatch.setattr("pitwall.pipeline.build_chain", lambda *a, **k: [provider])

    seen: list[tuple[str, int, int]] = []
    error: list[Exception] = []

    def worker():
        try:
            Pipeline(settings).run(_params(max_queries=3))
        except Exception as exc:  # pragma: no cover
            error.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        latest = repos.runs.latest(1)
        if latest:
            prog = repos.runs.get_progress(latest[0]["id"])
            if prog:
                seen.append((prog["stage"], prog["current"], prog["total"]))
        thread.join(timeout=0.01)
    thread.join(timeout=30)

    assert not error, error
    run = repos.runs.latest(1)[0]
    assert run["status"] == "done"
    assert run["finished_at"]
    assert run["queries_executed"] > 0
    assert repos.candidates.count() > 0

    # The contract is that the worker's writes are visible on the UI thread's own
    # connection. Whether the poller wins the race is down to machine speed — a
    # stubbed run can finish inside a single tick — so assert on the record that
    # survives rather than on having caught it mid-flight.
    final = repos.runs.get_progress(run["id"])
    assert final is not None, "the worker left no progress row for the UI to read"
    assert final["stage"], "the progress row carries no stage"
    if seen:
        assert {stage for stage, _, _ in seen} <= {
            "start", "build", "search", "extract", "heuristics", "llm", "done"
        }


def test_cancelling_from_another_thread_stops_the_run(settings, repos, monkeypatch,
                                                      write_inputs, company, role):
    """The ⏹ Cancel button writes a flag the running pipeline notices."""
    write_inputs([company], [role])
    sync_to_db(repos.conn, [company], [role])
    monkeypatch.setattr("pitwall.pipeline.build_chain", lambda *a, **k: [StubProvider()])
    pipe = Pipeline(settings, repos)

    run_id = repos.runs.start({})
    assert repos.runs.is_cancelled(run_id) is False
    repos.runs.cancel(run_id)
    assert repos.runs.is_cancelled(run_id) is True

    pipe._run_id = run_id
    assert pipe.cancelled() is True


# ─────────────────── CLI stages write run rows (§0.5) ────────────────────


def test_a_stage_run_opens_and_closes_its_own_run_row(pipeline, repos):
    """The `runs` table was empty after a whole real session because the CLI
    stages never went through the code path that records one."""
    stats = RunStats()
    params = _params()
    pipeline.build_queries(params)
    with pipeline.tracked(params, stats, "search"):
        pipeline.search(params, stats)

    rows = repos.runs.latest(1)
    assert len(rows) == 1
    assert rows[0]["command"] == "search"
    assert rows[0]["status"] == "done"
    assert rows[0]["finished_at"], "a closed run has a finish time"
    assert stats.run_id == rows[0]["id"]
    assert rows[0]["queries_executed"] == stats.queries_executed > 0


def test_progress_is_streamed_into_the_run_row(pipeline, repos):
    stats = RunStats()
    with pipeline.tracked(_params(), stats, "extract") as run_id:
        pipeline.progress("extract", "half way", 5, 10)
        prog = repos.runs.get_progress(run_id)
        assert prog["stage"] == "extract" and prog["current"] == 5


def test_a_failing_stage_still_leaves_a_terminal_run_row(pipeline, repos):
    """Not a row stuck at `running` — the failure has to be visible in history."""
    stats = RunStats()
    with pytest.raises(RuntimeError, match="boom"), pipeline.tracked(_params(), stats, "score"):
        raise RuntimeError("boom")

    row = repos.runs.latest(1)[0]
    assert row["status"] == "failed"
    assert "boom" in (row["error"] or "")
    assert row["finished_at"]


def test_the_stage_counters_land_on_the_run_row(pipeline, repos):
    stats = RunStats()
    with pipeline.tracked(_params(), stats, "search"):
        stats.queries_executed = 7
        stats.results_fetched = 42
        stats.candidates_new = 3
    row = repos.runs.latest(1)[0]
    assert (row["queries_executed"], row["results_fetched"], row["candidates_new"]) == (7, 42, 3)


def test_the_full_run_is_still_labelled_run(pipeline, repos):
    pipeline.run(_params())
    assert repos.runs.latest(1)[0]["command"] == "run"


def test_a_run_left_open_by_a_killed_process_is_closed_by_the_next_one(pipeline, repos):
    """SIGTERM does not raise in Python, so `finally` never runs and the row
    would stay `running` for ever.

    "Killed" is now proved by a heartbeat that stopped, not by a newer run
    existing: a second run starting is not evidence that the first one died,
    and treating it as such marked live runs interrupted while they worked.
    """
    orphan = repos.runs.start({}, "score")
    assert repos.runs.get(orphan)["status"] == "running"
    repos.conn.execute(
        "UPDATE run_progress SET updated_at = datetime('now', '-45 minutes')"
        " WHERE run_id = ?",
        (orphan,),
    )

    stats = RunStats()
    with pipeline.tracked(_params(), stats, "search"):
        pass

    closed = repos.runs.get(orphan)
    assert closed["status"] == "interrupted"
    assert closed["finished_at"]
    assert "without closing" in (closed["error"] or "")
    # and the live run is untouched
    assert repos.runs.get(stats.run_id)["status"] == "done"
