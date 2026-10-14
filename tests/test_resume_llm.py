"""Resuming step 5 over people an earlier run never read — the button on
**🔎 Find People → Unfinished AI analysis**.

Three different things leave a person found, rule-scored, and unread: a run the
owner stopped (`pending_llm`), a triage budget that passed over them
(`skipped_triage`), and an answer that came back unreadable (`needs_review`).
`awaiting_llm` has always returned all three. What was missing was a way to say
"finish those" without paying for the searching again — and a count, so the
page can say how much is left before the owner commits to it.

The rule these tests hold onto: resuming must not silently re-skip anybody. A
budget is a reason to defer a candidate, not a reason to refuse a run that was
started specifically to catch up on the deferred ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .paths import PITWALL_APP, PITWALL_PAGES, on_path  # noqa: TID252

ROOT = Path(__file__).resolve().parents[1]
on_path(PITWALL_APP)

from pitwall.inputs import sync_to_db  # noqa: E402
from pitwall.pipeline import Pipeline, RunParams  # noqa: E402

from .test_scoring_pipeline import seed_pending  # noqa: E402, TID252
from .test_triage import answering_client, spread  # noqa: E402, TID252

FIND_PAGE = PITWALL_PAGES / "2_🔎_Find_People.py"


@pytest.fixture()
def pipe(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    settings.zai_api_key = "sk-test"
    settings.llm_batch_size = 8
    return Pipeline(settings, repos)


# ─────────────────────── counting what is outstanding ────────────────────


def test_the_count_matches_the_rows_the_llm_stage_would_read(pipe, repos):
    spread(pipe, repos, [10.0, 50.0, 90.0])
    version = pipe.settings.prompt_version

    counts = repos.scores.awaiting_llm_counts(version)
    assert counts == {"pending_llm": 3}
    assert sum(counts.values()) == len(repos.scores.awaiting_llm(version))


def test_the_count_splits_deferred_from_unfinished(pipe, repos):
    """The two states the page shows separately, because they mean different
    things to the person reading them (§1.2)."""
    ids = spread(pipe, repos, [10.0, 20.0, 90.0])
    rows = repos.scores.awaiting_llm(pipe.settings.prompt_version)
    by_candidate = {r["candidate_id"]: r["id"] for r in rows}
    repos.scores.mark_triaged([by_candidate[ids[0]], by_candidate[ids[1]]])

    counts = repos.scores.awaiting_llm_counts(pipe.settings.prompt_version)
    assert counts == {"pending_llm": 1, "skipped_triage": 2}


def test_a_person_the_ai_has_read_is_not_counted(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    spread(pipe, repos, [60.0, 70.0])
    pipe.score_llm(RunParams(use_llm=True))

    assert repos.scores.awaiting_llm_counts(pipe.settings.prompt_version) == {}


def test_nothing_outstanding_counts_as_nothing(pipe, repos):
    seed_pending(pipe, repos, 0)
    assert repos.scores.awaiting_llm_counts(pipe.settings.prompt_version) == {}


# ───────────────────────── the triage waiver ─────────────────────────────


def test_ignore_triage_waives_a_configured_floor(pipe, repos, monkeypatch):
    _, seen = answering_client(monkeypatch)
    pipe.settings.llm_triage_min_heuristic = 50.0
    ids = spread(pipe, repos, [5.0, 20.0, 60.0, 90.0])

    stats = pipe.score_llm(RunParams(use_llm=True, ignore_triage=True))

    assert stats.llm_scored == 4
    assert stats.llm_triaged == 0
    assert sorted(sum(seen, [])) == sorted(ids)


def test_ignore_triage_waives_a_configured_cap(pipe, repos, monkeypatch):
    _, seen = answering_client(monkeypatch)
    pipe.settings.llm_triage_top_n = 1
    ids = spread(pipe, repos, [10.0, 80.0, 95.0])

    stats = pipe.score_llm(RunParams(use_llm=True, ignore_triage=True))

    assert stats.llm_scored == 3
    assert sorted(sum(seen, [])) == sorted(ids)


def test_the_waiver_is_off_by_default(pipe, repos, monkeypatch):
    """A normal run still honours the budget it was configured with."""
    answering_client(monkeypatch)
    pipe.settings.llm_triage_min_heuristic = 50.0
    spread(pipe, repos, [5.0, 90.0])

    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 1 and stats.llm_triaged == 1


# ──────────────────────────── resume_llm ─────────────────────────────────


def test_resuming_reads_the_backlog_and_leaves_nobody_waiting(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    spread(pipe, repos, [30.0, 60.0, 90.0])

    stats = pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True))

    assert stats.llm_scored == 3
    assert repos.scores.awaiting_llm_counts(pipe.settings.prompt_version) == {}


def test_resuming_picks_up_the_people_a_budget_passed_over(pipe, repos, monkeypatch):
    """The whole point: `skipped_triage` is deferred, not discarded."""
    _, seen = answering_client(monkeypatch)
    pipe.settings.llm_triage_top_n = 1
    ids = spread(pipe, repos, [10.0, 95.0])
    pipe.score_llm(RunParams(use_llm=True))          # one scored, one deferred
    assert repos.scores.awaiting_llm_counts(pipe.settings.prompt_version) == {
        "skipped_triage": 1
    }

    pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True))

    assert sorted(sum(seen, [])) == sorted(ids)
    assert repos.scores.awaiting_llm_counts(pipe.settings.prompt_version) == {}


def test_resuming_searches_for_nobody_new(pipe, repos, monkeypatch):
    """No queries, no SERP rows, no new candidates — it is step 5 alone, which
    is why the page reports AI calls rather than searches when it finishes."""
    answering_client(monkeypatch)
    spread(pipe, repos, [60.0])

    stats = pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True))

    assert stats.queries_executed == 0
    assert stats.results_fetched == 0
    assert stats.candidates_new == 0
    assert stats.llm_calls == 1


def test_a_limit_reads_the_best_first_and_leaves_the_rest_queued(pipe, repos, monkeypatch):
    _, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [10.0, 50.0, 95.0])

    stats = pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True, limit=1))

    assert stats.llm_scored == 1
    assert sum(seen, []) == [ids[2]]
    assert sum(repos.scores.awaiting_llm_counts(pipe.settings.prompt_version).values()) == 2


def test_resuming_gets_its_own_run_row(pipe, repos, monkeypatch):
    """Analytics counts the cost of a resume like any other run — a stage that
    spends money off the books is how v1 ended up with an empty `runs` table."""
    answering_client(monkeypatch)
    spread(pipe, repos, [60.0])

    stats = pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True))

    row = repos.runs.get(stats.run_id)
    assert row["command"] == "resume-llm"
    assert row["status"] == "done"
    assert row["llm_calls"] == 1


def test_a_failing_provider_leaves_the_backlog_intact(pipe, repos, monkeypatch):
    """Nothing is consumed by trying: if the AI cannot be reached, the same
    people are still waiting and the button still offers to read them."""
    from .test_llm_contract import FakeClient  # noqa: TID252

    client = FakeClient([RuntimeError("Error code: 429 - quota exceeded")])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: __import__(
            "pitwall.scoring.llm", fromlist=["LLMScorer"]
        ).LLMScorer(settings, provider="zai", client=client),
    )
    monkeypatch.setattr("pitwall.pipeline.ollama_reachable", lambda *a, **k: False)
    spread(pipe, repos, [60.0, 70.0])

    stats = pipe.resume_llm(RunParams(use_llm=True, ignore_triage=True))

    assert stats.llm_scored == 0
    assert sum(repos.scores.awaiting_llm_counts(pipe.settings.prompt_version).values()) == 2


# ─────────────────── reading only the people you chose ───────────────────


def test_a_selection_narrows_the_backlog_to_those_people(pipe, repos):
    ids = spread(pipe, repos, [10.0, 50.0, 95.0])
    version = pipe.settings.prompt_version

    rows = repos.scores.awaiting_llm(version, candidate_ids=[ids[0], ids[2]])

    assert {r["candidate_id"] for r in rows} == {ids[0], ids[2]}
    assert repos.scores.awaiting_llm_counts(version, candidate_ids=[ids[0]]) == {
        "pending_llm": 1
    }


def test_choosing_nobody_returns_nobody(pipe, repos):
    """An empty selection is a selection, not "no filter" — a button that read
    the whole database because nothing was ticked would be a nasty surprise."""
    spread(pipe, repos, [10.0, 95.0])
    version = pipe.settings.prompt_version

    assert repos.scores.awaiting_llm(version, candidate_ids=[]) == []
    assert repos.scores.awaiting_llm_counts(version, candidate_ids=[]) == {}
    assert repos.scores.awaiting_llm_candidates(version, candidate_ids=[]) == []


def test_the_backlog_is_listed_as_people_best_first(pipe, repos):
    """One person can hold several score rows — one per company/role pair they
    were matched against. What the page ticks and counts is people."""
    ids = spread(pipe, repos, [10.0, 95.0, 50.0])

    assert repos.scores.awaiting_llm_candidates(pipe.settings.prompt_version) == [
        ids[1], ids[2], ids[0]
    ]


def test_a_person_with_two_pending_rows_is_listed_once(pipe, repos):
    from pitwall.models import Score

    ids = spread(pipe, repos, [40.0])
    company = repos.companies.all()[0]
    repos.scores.upsert(
        Score(candidate_id=ids[0], company_id=company["id"], role_id=None,
              heuristic_score=80.0, verdict="pending_llm",
              prompt_version=pipe.settings.prompt_version)
    )
    version = pipe.settings.prompt_version

    assert len(repos.scores.awaiting_llm(version)) == 2
    assert repos.scores.awaiting_llm_candidates(version) == [ids[0]]


def test_a_run_scoped_to_a_selection_leaves_everyone_else_waiting(pipe, repos, monkeypatch):
    """The People page's whole promise: these people, not the database."""
    _, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [10.0, 50.0, 95.0])

    stats = pipe.resume_llm(
        RunParams(use_llm=True, ignore_triage=True, candidate_ids=[ids[0]])
    )

    assert stats.llm_scored == 1
    assert sum(seen, []) == [ids[0]]
    assert repos.scores.awaiting_llm_candidates(pipe.settings.prompt_version) == [
        ids[2], ids[1]
    ]


def test_a_selection_of_hundreds_is_one_query_not_a_variable_per_person(pipe, repos):
    """`json_each`, not a placeholder each: SQLite caps bound variables, and a
    'select all' over the whole backlog is exactly where that ceiling is met."""
    ids = spread(pipe, repos, [50.0] * 3)
    huge = [*ids, *range(10_000, 12_000)]

    rows = repos.scores.awaiting_llm(pipe.settings.prompt_version, candidate_ids=huge)

    assert {r["candidate_id"] for r in rows} == set(ids)


# ───────────────────────────── the page itself ───────────────────────────


def _seed_ui_db(waiting: int = 0, read: int = 0, domain: bool = False) -> None:
    """A company, a role, `waiting` people no AI has read, and `read` it has."""
    from pitwall.config import get_settings
    from pitwall.db import close_all, open_repos

    settings = get_settings()
    repos = open_repos(settings)
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    repos.conn.execute(
        "INSERT INTO roles (title, family) VALUES ('Data Scientist','data_science')"
    )
    for i in range(waiting):
        cid, _ = repos.candidates.upsert({
            "linkedin_slug": f"p{i}", "profile_url": f"https://x/in/p{i}",
            "full_name": f"Person {i}",
        })
        repos.conn.execute(
            "INSERT INTO scores (candidate_id, company_id, role_id, heuristic_score,"
            " final_score, verdict, prompt_version) VALUES (?,1,1,60,30,'pending_llm',?)",
            (cid, settings.prompt_version),
        )
    for i in range(read):
        cid, _ = repos.candidates.upsert({
            "linkedin_slug": f"done{i}", "profile_url": f"https://x/in/done{i}",
            "full_name": f"Read Person {i}", "current_company": "Alpine",
        })
        repos.conn.execute(
            "INSERT INTO scores (candidate_id, company_id, role_id, heuristic_score,"
            " llm_score, final_score, verdict, prompt_version)"
            " VALUES (?,1,1,60,90,88,'strong',?)",
            (cid, settings.prompt_version),
        )
    if domain:
        repos.conn.execute(
            "INSERT INTO company_domains (company_id, domain, source, pattern,"
            " pattern_sample_size) VALUES (1,'alpine-racing.com','resolved','first.last',3)"
        )
    repos.conn.commit()
    close_all()


def _run_page():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(FIND_PAGE), default_timeout=60).run()


PEOPLE_PAGE = PITWALL_PAGES / "3_👤_People.py"


def _run_people_page():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(PEOPLE_PAGE), default_timeout=60).run()


