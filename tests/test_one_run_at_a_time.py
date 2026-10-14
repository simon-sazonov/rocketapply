"""Two runs at once — what pressing the button twice actually did.

The report was "I pressed it more than once before the first finished". Three
separate faults came out of that, and each has a test here:

1. `tracked()` closed every older `running` row when it opened a new one, on the
   theory that an open row means a dead process. Start a second run and the
   first — still scoring people — was marked `interrupted` while it worked.
2. The page followed "the newest run row" rather than the run it started, so
   the second run's finish announced itself over the top of the first, which
   carried on spending the allowance invisibly.
3. Nothing refused the second run in the first place: both read the same
   `awaiting_llm` rows, so every person in the overlap was read, and paid for,
   twice.

The rule: one AI run at a time, and staleness is decided by a heartbeat, never
by "a newer row exists".
"""

from __future__ import annotations

import pytest

from pitwall.inputs import sync_to_db
from pitwall.pipeline import Pipeline, RunParams, RunStats

from .test_triage import answering_client, spread  # noqa: TID252


@pytest.fixture()
def pipe(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    settings.zai_api_key = "sk-test"
    return Pipeline(settings, repos)


def _age(repos, run_id: int, minutes: int) -> None:
    """Backdate a run's heartbeat, as an abandoned process would leave it."""
    repos.conn.execute(
        "UPDATE run_progress SET updated_at = datetime('now', ?) WHERE run_id = ?",
        (f"-{minutes} minutes", run_id),
    )
    repos.conn.execute(
        "UPDATE runs SET started_at = datetime('now', ?) WHERE id = ?",
        (f"-{minutes} minutes", run_id),
    )


# ───────────────── a live run is not a stale run (fault 1) ───────────────


def test_a_second_run_does_not_close_the_first_one(pipe, repos):
    """The bug, exactly: run A is working, run B starts, A is marked dead."""
    stats_a = RunStats()
    with pipe.tracked(RunParams(), stats_a, "resume-llm"):
        pipe.progress("llm", "reading people", 3, 100)

        stats_b = RunStats()
        with pipe.tracked(RunParams(), stats_b, "resume-llm"):
            pass

        assert repos.runs.get(stats_a.run_id)["status"] == "running"

    assert repos.runs.get(stats_a.run_id)["status"] == "done"


def test_a_run_whose_process_died_is_still_swept(pipe, repos):
    """The behaviour that was worth keeping: a row nobody will ever close."""
    stats = RunStats()
    with pipe.tracked(RunParams(), stats, "resume-llm"):
        pass
    repos.conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (stats.run_id,))
    _age(repos, stats.run_id, 45)

    with pipe.tracked(RunParams(), RunStats(), "resume-llm"):
        pass

    row = repos.runs.get(stats.run_id)
    assert row["status"] == "interrupted"
    assert "without closing" in row["error"]


def test_the_sweep_leaves_a_recent_heartbeat_alone(pipe, repos):
    stats = RunStats()
    with pipe.tracked(RunParams(), stats, "resume-llm"):
        pass
    repos.conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (stats.run_id,))
    _age(repos, stats.run_id, 5)          # quiet for five minutes, not thirty

    assert repos.runs.reset_stale() == 0
    assert repos.runs.get(stats.run_id)["status"] == "running"


# ──────────────── "is something running?" (faults 2 and 3) ───────────────


def test_nothing_running_means_nothing_running(repos):
    assert repos.runs.active() is None


def test_a_working_run_is_visible_to_every_other_page(pipe, repos):
    """What the buttons ask before they let a second run start. It has to be
    the database: another tab, another page and the CLI all count."""
    stats = RunStats()
    with pipe.tracked(RunParams(), stats, "resume-llm"):
        pipe.progress("llm", "42 of 300 people read", 42, 300)

        busy = repos.runs.active()
        assert busy is not None
        assert busy["id"] == stats.run_id
        assert busy["message"] == "42 of 300 people read"

    assert repos.runs.active() is None


def test_an_abandoned_run_stops_counting_as_active(pipe, repos):
    """Otherwise one crash locks the button that starts the next run for ever."""
    stats = RunStats()
    with pipe.tracked(RunParams(), stats, "resume-llm"):
        pass
    repos.conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (stats.run_id,))
    _age(repos, stats.run_id, 10)

    assert repos.runs.active() is None


def test_the_active_run_is_the_newest_of_several(pipe, repos):
    first = RunStats()
    with pipe.tracked(RunParams(), first, "resume-llm"):
        pipe.progress("llm", "first", 1, 2)
        second = RunStats()
        with pipe.tracked(RunParams(), second, "resume-llm"):
            pipe.progress("llm", "second", 1, 2)
            assert repos.runs.active()["id"] == second.run_id


def test_a_rocketapply_job_search_is_not_a_pitwall_run(repos):
    """PitWall's checks ask about networking runs only, and RocketApply's about
    job searches only: one app's run must not lock, or be stopped by, the other."""
    from pitwall.db import JOB_SEARCH_COMMANDS, NETWORKING_COMMANDS

    jobs = repos.runs.start({}, "job-search")
    repos.runs.progress(jobs, "fetch", "Adzuna", 1, 9)
    assert repos.runs.active(commands=NETWORKING_COMMANDS) is None
    assert repos.runs.latest(1, commands=NETWORKING_COMMANDS) == []
    assert repos.runs.active(commands=JOB_SEARCH_COMMANDS)["id"] == jobs

    people = repos.runs.start({}, "resume-llm")
    repos.runs.progress(people, "llm", "reading people", 1, 9)
    assert repos.runs.active(commands=NETWORKING_COMMANDS)["id"] == people
    assert repos.runs.active(commands=JOB_SEARCH_COMMANDS)["id"] == jobs
    assert [r["id"] for r in repos.runs.latest(5, commands=JOB_SEARCH_COMMANDS)] == [jobs]
    assert repos.runs.latest(1)[0]["id"] == people          # unfiltered, as before


def test_every_run_kind_belongs_to_one_app():
    """A new `tracked(..., "kind")` that is in neither list would be invisible to
    both apps' "is anything running?" checks."""
    import re
    from pathlib import Path

    from pitwall.db import JOB_SEARCH_COMMANDS, NETWORKING_COMMANDS
    from pitwall.jobsearch.run import COMMAND

    assert not set(NETWORKING_COMMANDS) & set(JOB_SEARCH_COMMANDS)
    assert COMMAND in JOB_SEARCH_COMMANDS
    root = Path(__file__).resolve().parents[1]
    kinds = set()
    for folder in ("src", "apps"):
        for py in (root / folder).rglob("*.py"):
            if "_archive" in py.parts:
                continue
            text = py.read_text(encoding="utf-8")
            kinds |= set(re.findall(r'tracked\([^)]*?"([a-z-]+)"\)', text))
            kinds |= set(re.findall(r'runs\.start\([^)]*?"([a-z-]+)"\)', text))
    assert kinds, "the pattern found no run kinds at all"
    assert kinds <= set(NETWORKING_COMMANDS) | set(JOB_SEARCH_COMMANDS)


# ─────────────────── the double-read the guard prevents ──────────────────


def test_two_runs_over_one_backlog_read_everyone_twice(pipe, repos, monkeypatch):
    """Why the guard exists, stated as money: nothing in the pipeline stops the
    second run reading the same people, so the UI has to."""
    _, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [90.0, 80.0])
    params = RunParams(use_llm=True, ignore_triage=True)

    first = pipe.repos.scores.awaiting_llm(pipe.settings.prompt_version)
    second = pipe.repos.scores.awaiting_llm(pipe.settings.prompt_version)
    assert [r["id"] for r in first] == [r["id"] for r in second]

    pipe.score_llm(params)
    assert sorted(sum(seen, [])) == sorted(ids)


# ──────────────── the reason a run that read nobody gives ────────────────


def test_an_exhausted_chain_records_why_not_just_that(pipe, repos, monkeypatch):
    """"LLM chain exhausted" told the owner nothing. The message the page shows
    has to carry the provider's own words — that is where "rate limit reached"
    and "try again in 8m" live."""
    from pitwall.scoring.llm import LLMScorer

    from .test_llm_contract import FakeClient  # noqa: TID252

    client = FakeClient([RuntimeError("Error code: 429 - rate limit reached, per day")])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: LLMScorer(settings, provider="zai", client=client),
    )
    monkeypatch.setattr("pitwall.pipeline.ollama_reachable", lambda *a, **k: False)
    spread(pipe, repos, [90.0])

    stats = pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True))

    assert stats.llm_calls == 0
    llm_events = [e for e in repos.runs.events(stats.run_id) if e["stage"] == "llm"]
    assert llm_events, "the run has to say what happened in the LLM stage"
    said = llm_events[-1]["message"]
    assert "no AI provider could be reached" in said
    assert "rate limit reached" in said        # the provider's own words


# ─────────────────────── what the page does about it ─────────────────────


def _busy_run(minutes_ago: float = 0.0) -> int:
    """A run row that looks like it is working right now."""
    from pitwall.config import get_settings
    from pitwall.db import close_all, open_repos

    repos = open_repos(get_settings())
    run_id = repos.runs.start({}, "resume-llm")
    repos.runs.progress(run_id, "llm", "40 of 300 people read", 40, 300)
    if minutes_ago:
        repos.conn.execute(
            "UPDATE run_progress SET updated_at = datetime('now', ?) WHERE run_id = ?",
            (f"-{minutes_ago:g} minutes", run_id),
        )
    repos.conn.commit()
    close_all()
    return run_id


