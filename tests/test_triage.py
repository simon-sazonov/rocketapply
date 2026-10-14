"""LLM triage — §1.2.

Not every candidate is worth a verdict. Two gates, both off by default so the
v1 behaviour is unchanged:

* `LLM_TRIAGE_MIN_HEURISTIC` — a floor on the heuristic score;
* `LLM_TRIAGE_TOP_N` — a hard budget per run, highest heuristic first.

The state matters as much as the saving. A skipped candidate is marked
`skipped_triage`, not left at `pending_llm`, because "we chose not to score this
person" and "this run did not get that far" are different facts and the UI has
to be able to tell them apart. They stay eligible for a later run.
"""

from __future__ import annotations

import json

import pytest

from pitwall.config import Settings
from pitwall.inputs import sync_to_db
from pitwall.models import Score
from pitwall.pipeline import Pipeline, RunParams
from pitwall.scoring.llm import LLMScorer

from .test_llm_contract import GOOD, FakeClient  # noqa: TID252
from .test_scoring_pipeline import seed_pending  # noqa: TID252


@pytest.fixture()
def pipe(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    settings.zai_api_key = "sk-test"
    settings.llm_batch_size = 8
    return Pipeline(settings, repos)


def spread(pipe, repos, scores: list[float]) -> list[int]:
    """Seed candidates whose heuristic scores are exactly `scores`."""
    ids = seed_pending(pipe, repos, len(scores))
    company = repos.companies.all()[0]
    role = repos.roles.all()[0]
    for cid, score in zip(ids, scores, strict=True):
        repos.scores.upsert(
            Score(candidate_id=cid, company_id=company["id"], role_id=role["id"],
                  heuristic_score=score, verdict="pending_llm",
                  prompt_version=pipe.settings.prompt_version)
        )
    return ids


def answering_client(monkeypatch):
    """A client that answers whatever candidate ids it is asked about."""
    seen: list[list[int]] = []

    class Client(FakeClient):
        def __init__(self):
            super().__init__([])

        def _create(self, **kwargs):
            from types import SimpleNamespace

            ids = [
                int(line.split()[1].rstrip(":"))
                for line in kwargs["messages"][1]["content"].splitlines()
                if line.strip().startswith("candidate_id")
            ]
            seen.append(ids)
            self.calls.append(kwargs)
            body = json.dumps(
                {"results": [{**GOOD["results"][0], "candidate_id": i} for i in ids]}
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=body))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            )

    client = Client()
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: LLMScorer(settings, provider="zai", client=client),
    )
    return client, seen


# ───────────────────────────── off by default ────────────────────────────


def test_triage_is_off_by_default():
    s = Settings(_env_file=None)
    assert s.llm_triage_min_heuristic is None
    assert s.llm_triage_top_n is None


def test_with_triage_off_everyone_is_scored(pipe, repos, monkeypatch):
    client, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [5.0, 20.0, 60.0, 90.0])
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_scored == 4
    assert stats.llm_triaged == 0
    assert sorted(sum(seen, [])) == sorted(ids)


# ──────────────────────────── the heuristic floor ────────────────────────


def test_the_floor_keeps_the_bottom_of_the_list_out_of_the_llm(pipe, repos, monkeypatch):
    client, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [5.0, 20.0, 60.0, 90.0])
    stats = pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=50.0))

    assert stats.llm_scored == 2
    assert stats.llm_triaged == 2
    scored = sum(seen, [])
    assert set(scored) == {ids[2], ids[3]}


def test_the_floor_is_inclusive(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    spread(pipe, repos, [50.0])
    stats = pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=50.0))
    assert stats.llm_scored == 1 and stats.llm_triaged == 0


def test_the_reduction_is_logged(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    spread(pipe, repos, [5.0, 90.0])
    stats = pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=50.0))
    assert any("triage kept 1 of 2" in n for n in stats.notes)


# ──────────────────────────────── the cap ────────────────────────────────


def test_the_cap_takes_the_highest_heuristics_first(pipe, repos, monkeypatch):
    client, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [10.0, 80.0, 30.0, 95.0, 55.0])
    stats = pipe.score_llm(RunParams(use_llm=True, triage_top_n=2))

    assert stats.llm_scored == 2
    assert set(sum(seen, [])) == {ids[3], ids[1]}   # 95 and 80


def test_the_two_gates_compose(pipe, repos, monkeypatch):
    client, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [10.0, 80.0, 30.0, 95.0, 55.0])
    stats = pipe.score_llm(
        RunParams(use_llm=True, triage_min_heuristic=50.0, triage_top_n=2)
    )
    assert stats.llm_scored == 2
    assert set(sum(seen, [])) == {ids[3], ids[1]}
    assert stats.llm_triaged == 3


def test_a_cap_larger_than_the_backlog_skips_nobody(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    spread(pipe, repos, [10.0, 80.0])
    stats = pipe.score_llm(RunParams(use_llm=True, triage_top_n=99))
    assert stats.llm_triaged == 0 and stats.llm_scored == 2


# ─────────────────────── the state, and coming back ──────────────────────


def test_a_skipped_candidate_gets_its_own_visible_state(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    ids = spread(pipe, repos, [5.0, 90.0])
    pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=50.0))

    low = repos.scores.for_candidate(ids[0])[0]
    assert low["verdict"] == "skipped_triage", "not pending_llm — the distinction is the point"
    assert low["heuristic_score"] == 5.0, "the heuristic score is kept"
    assert low["llm_score"] is None


def test_a_skipped_candidate_is_still_eligible_next_run(pipe, repos, monkeypatch):
    client, seen = answering_client(monkeypatch)
    ids = spread(pipe, repos, [5.0, 90.0])

    pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=50.0))
    assert set(sum(seen, [])) == {ids[1]}

    # a later run with a lower bar picks the skipped one up
    seen.clear()
    stats = pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=0.0))
    assert set(sum(seen, [])) == {ids[0]}
    assert stats.llm_scored == 1
    assert repos.scores.for_candidate(ids[0])[0]["verdict"] != "skipped_triage"


def test_triage_never_touches_a_candidate_that_already_has_a_verdict(pipe, repos, monkeypatch):
    answering_client(monkeypatch)
    ids = spread(pipe, repos, [90.0])
    pipe.score_llm(RunParams(use_llm=True))
    before = repos.scores.for_candidate(ids[0])[0]
    assert before["llm_score"] is not None

    pipe.score_llm(RunParams(use_llm=True, triage_min_heuristic=99.0))
    after = repos.scores.for_candidate(ids[0])[0]
    assert after["verdict"] == before["verdict"]
    assert after["llm_score"] == before["llm_score"]


