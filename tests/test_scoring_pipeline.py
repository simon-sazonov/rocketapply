"""The §9.2 final-score formula, the LLM stage, and structured logging."""

from __future__ import annotations

import json

import pytest

from pitwall.inputs import sync_to_db
from pitwall.models import LLMVerdict
from pitwall.pipeline import Pipeline, RunParams

from .test_llm_contract import FakeClient  # noqa: TID252


def verdict(**kw):
    base = {
        "candidate_id": 1, "is_person_profile": True, "is_current_employee": True,
        "company_match": "exact", "role_match": "exact", "seniority": "mid",
        "score": 80.0, "confidence": 0.9,
    }
    base.update(kw)
    return LLMVerdict(**base)


@pytest.fixture()
def pipe(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    return Pipeline(settings, repos)


# ─────────────────────── the §9.2 blend and penalties ────────────────────


def test_the_blend_is_thirty_seventy(pipe):
    assert pipe.final_score(50.0, verdict(score=100.0)) == pytest.approx(0.30 * 50 + 0.70 * 100)
    assert pipe.final_score(0.0, verdict(score=80.0)) == pytest.approx(56.0)


def test_a_former_employee_is_cut_to_thirty_five_percent(pipe):
    base = pipe.final_score(60.0, verdict())
    former = pipe.final_score(60.0, verdict(is_current_employee=False))
    assert former == pytest.approx(base * 0.35)


def test_same_name_different_org_is_cut_to_ten_percent(pipe):
    base = pipe.final_score(60.0, verdict())
    wrong_org = pipe.final_score(60.0, verdict(company_match="same_name_different_org"))
    assert wrong_org == pytest.approx(base * 0.10)
    assert wrong_org < 15  # the plan's rubric puts these below 15


def test_an_unrelated_role_is_cut_to_forty_percent(pipe):
    base = pipe.final_score(60.0, verdict())
    unrelated = pipe.final_score(60.0, verdict(role_match="unrelated"))
    assert unrelated == pytest.approx(base * 0.40)


def test_low_confidence_is_cut_to_eighty_percent(pipe):
    base = pipe.final_score(60.0, verdict(confidence=0.9))
    unsure = pipe.final_score(60.0, verdict(confidence=0.49))
    assert unsure == pytest.approx(base * 0.80)


def test_the_penalties_compound(pipe):
    base = pipe.final_score(60.0, verdict())
    everything = pipe.final_score(
        60.0,
        verdict(is_current_employee=False, company_match="same_name_different_org",
                role_match="unrelated", confidence=0.2),
    )
    # final_score rounds to 2dp, so compare with that tolerance
    assert everything == pytest.approx(base * 0.35 * 0.10 * 0.40 * 0.80, abs=0.01)


def test_a_non_person_profile_is_capped(pipe):
    assert pipe.final_score(100.0, verdict(is_person_profile=False, score=100)) <= 10.0


def test_the_final_score_stays_in_range(pipe):
    assert pipe.final_score(100.0, verdict(score=100.0)) == 100.0
    assert pipe.final_score(0.0, verdict(score=0.0)) == 0.0


@pytest.mark.parametrize(
    ("score", "band"),
    [(90, "strong"), (75, "strong"), (74, "possible"), (55, "possible"),
     (54, "weak"), (35, "weak"), (34, "reject"), (0, "reject")],
)
def test_the_verdict_bands(settings, score, band):
    assert settings.band_for(score) == band


# ─────────────────────────── the LLM stage ───────────────────────────────


def reply(*items):
    return json.dumps({"results": list(items)})


def seed_pending(pipe, repos, n=3):
    """Candidates that already passed the heuristic gate, with their evidence.

    The evidence rows matter: `_candidate_context` walks
    candidates -> candidate_sources -> search_results -> queries to work out
    which company×role pair a person should be judged against, so a candidate
    with no evidence is invisible to the heuristic stage.
    """
    from pitwall.models import Score

    company = repos.companies.all()[0]
    role = repos.roles.all()[0]
    conn = repos.conn
    conn.execute(
        "INSERT INTO queries (id, company_id, role_id, template_id, query_text, query_hash,"
        " status) VALUES (1,?,?,'profile_exact','q','h','done') ON CONFLICT DO NOTHING",
        (company["id"], role["id"]),
    )
    ids = []
    for i in range(n):
        cid, _ = repos.candidates.upsert(
            {"linkedin_slug": f"p{i}", "profile_url": f"https://www.linkedin.com/in/p{i}",
             "full_name": f"Person {i}", "headline": f"Data Scientist at McLaren Racing {i}"}
        )
        conn.execute(
            "INSERT INTO search_results (id, query_id, provider, rank, url, canonical_url,"
            " title, snippet, processed) VALUES (?,1,'ddgs',?,?,?,?,?,1)",
            (i + 1, i, f"https://www.linkedin.com/in/p{i}", f"https://linkedin.com/in/p{i}",
             f"Person {i} - Data Scientist - McLaren Racing | LinkedIn",
             "Data Scientist at McLaren Racing · Woking, England, United Kingdom"),
        )
        repos.candidates.link_source(cid, i + 1)
        repos.scores.upsert(
            Score(candidate_id=cid, company_id=company["id"], role_id=role["id"],
                  heuristic_score=60.0, verdict="pending_llm",
                  prompt_version=pipe.settings.prompt_version)
        )
        ids.append(cid)
    return ids


def test_the_llm_stage_writes_blended_scores_and_bands(pipe, repos, monkeypatch):
    ids = seed_pending(pipe, repos, 2)
    client = FakeClient([reply(
        {"candidate_id": ids[0], "is_person_profile": True, "is_current_employee": True,
         "company_match": "exact", "role_match": "exact", "seniority": "mid",
         "score": 95, "confidence": 0.9, "reasons": ["title says so"], "red_flags": []},
        {"candidate_id": ids[1], "is_person_profile": True, "is_current_employee": True,
         "company_match": "same_name_different_org", "role_match": "exact", "seniority": "mid",
         "score": 90, "confidence": 0.9, "reasons": ["wrong McLaren"],
         "red_flags": ["McLaren Health Care"]},
    )])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: __import__("pitwall.scoring.llm", fromlist=["LLMScorer"]).LLMScorer(
            settings, client=client
        ),
    )
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_calls == 1
    assert stats.llm_scored == 2

    scores = {s["candidate_id"]: s for s in
              [repos.scores.for_candidate(i)[0] for i in ids]}
    good, bad = scores[ids[0]], scores[ids[1]]
    assert good["verdict"] == "strong"
    assert good["final_score"] == pytest.approx(0.30 * 60 + 0.70 * 95)
    assert "title says so" in json.loads(good["reasons"])
    assert bad["verdict"] == "reject"
    assert bad["final_score"] < 15
    assert "McLaren Health Care" in json.loads(bad["red_flags"])


def test_the_llm_stage_consumes_daily_quota(pipe, repos, monkeypatch):
    ids = seed_pending(pipe, repos, 1)
    client = FakeClient([reply({"candidate_id": ids[0], "score": 80, "confidence": 0.8})])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: __import__("pitwall.scoring.llm", fromlist=["LLMScorer"]).LLMScorer(
            settings, client=client
        ),
    )
    pipe.score_llm(RunParams(use_llm=True))
    assert repos.quota.used_today("llm:zai") == 1


def test_a_quota_wall_saves_progress_instead_of_crashing(pipe, repos, monkeypatch):
    seed_pending(pipe, repos, 2)
    client = FakeClient([RuntimeError("Error code: 429 - quota exceeded")])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: __import__("pitwall.scoring.llm", fromlist=["LLMScorer"]).LLMScorer(
            settings, client=client
        ),
    )
    monkeypatch.setattr("pitwall.pipeline.ollama_reachable", lambda *a, **k: False)
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_scored == 0
    assert any("quota" in note for note in stats.notes)
    # nobody was dropped: they are still queued for the next run
    assert len(repos.scores.awaiting_llm(pipe.settings.prompt_version)) == 2


def test_the_daily_cap_stops_the_stage_before_it_starts(pipe, repos, monkeypatch):
    # The provider has to be *configured* for the cap to be the reason it is
    # skipped — otherwise v2's chain drops it for the missing key first, which
    # is true but is a different message (§0.4).
    pipe.settings.zai_api_key = "sk-test"
    seed_pending(pipe, repos, 1)
    repos.quota.consume("llm:zai", pipe.settings.daily_llm_request_cap)
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_calls == 0
    assert any("cap" in note for note in stats.notes)


def test_an_unparseable_reply_marks_needs_review_not_deletion(pipe, repos, monkeypatch):
    ids = seed_pending(pipe, repos, 1)
    client = FakeClient(["not json", "still not json"])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: __import__("pitwall.scoring.llm", fromlist=["LLMScorer"]).LLMScorer(
            settings, client=client
        ),
    )
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_needs_review == 1
    row = repos.scores.for_candidate(ids[0])[0]
    assert row["verdict"] == "needs_review"
    assert repos.candidates.get_by_slug("p0") is not None


def test_disabling_the_llm_is_a_clean_no_op(pipe, repos):
    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=False))
    assert stats.llm_calls == 0 and stats.notes == []


def test_scores_are_versioned_by_prompt(pipe, repos):
    """A prompt change must not silently overwrite history (§9.3)."""
    from pitwall.models import Score

    cid, _ = repos.candidates.upsert({"linkedin_slug": "x", "profile_url": "u"})
    for version, score in (("v1", 50.0), ("v2", 80.0)):
        repos.scores.upsert(
            Score(candidate_id=cid, company_id=None, role_id=None, heuristic_score=score,
                  final_score=score, verdict="possible", prompt_version=version)
        )
    rows = repos.scores.for_candidate(cid)
    assert {r["prompt_version"] for r in rows} == {"v1", "v2"}


# ────────────────────────────── logging ──────────────────────────────────


def test_structured_logging_writes_one_json_line_per_event(settings):
    import logging

    from pitwall.logging_setup import setup_logging

    log = setup_logging(settings, console_level=logging.CRITICAL, force=True)
    log.info("stage finished", extra={"stage": "search", "run_id": 7})
    for handler in log.handlers:
        handler.flush()

    lines = [x for x in settings.log_file.read_text(encoding="utf-8").splitlines() if x.strip()]
    payload = json.loads(lines[-1])
    assert payload["message"] == "stage finished"
    assert payload["stage"] == "search"
    assert payload["run_id"] == 7
    assert payload["level"] == "INFO"
    assert "ts" in payload


def test_exceptions_are_captured_in_the_log(settings):
    import logging

    from pitwall.logging_setup import setup_logging

    log = setup_logging(settings, console_level=logging.CRITICAL, force=True)
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("stage failed", extra={"stage": "llm"})
    for handler in log.handlers:
        handler.flush()

    payload = json.loads(settings.log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert payload["level"] == "ERROR"
    assert "boom" in payload["exc"]


# ───────────────────────────── the eval set ──────────────────────────────


def test_the_eval_set_exists_and_is_fully_labelled():
    """§12.2 — tuning without a labelled set is guessing."""
    import csv

    from .conftest import FIXTURES  # noqa: TID252

    rows = list((FIXTURES / "eval_set.csv").open(encoding="utf-8-sig").readlines())
    assert len(rows) >= 30, "the plan asks for ~60 hand-labelled candidates"

    parsed = list(csv.DictReader((FIXTURES / "eval_set.csv").open(encoding="utf-8-sig")))
    assert len(parsed) == 60
    labels = {r["relevant"].strip().lower() for r in parsed}
    assert labels <= {"yes", "no"}, f"unlabelled or malformed rows: {labels}"
    assert all(r["serp_title"] for r in parsed), "every row needs its raw evidence"
    yes = sum(1 for r in parsed if r["relevant"] == "yes")
    assert 10 <= yes <= 50, "a set that is all-yes or all-no measures nothing"


def test_the_configured_threshold_clears_the_recall_floor(settings):
    """The shipped `heuristic_threshold` must not be throwing away real people."""
    import csv

    from pitwall.models import Company, Role
    from pitwall.scoring.heuristics import passes, score_candidate

    from .conftest import FIXTURES  # noqa: TID252

    rows = list(csv.DictReader((FIXTURES / "eval_set.csv").open(encoding="utf-8-sig")))
    tp = fn = 0
    for row in rows:
        result = score_candidate(
            {
                "profile_url": row["profile_url"],
                "full_name": row["full_name"],
                "headline": row["serp_snippet"],
                "location": row["location"] or None,
                "evidence_count": int(row["evidence_count"] or 0),
                "current_company": "",
            },
            Company(
                name=row["target_company"] or "unknown",
                aliases=row["company_aliases"],
                negative_keywords=row["company_negatives"],
            ),
            Role(
                title=row["target_role"] or "unknown",
                aliases=row["role_aliases"],
                family=row["role_family"] or "data_science",
                seniority=row["role_seniority"],
                keywords=row["role_keywords"],
            ),
            serp_title=row["serp_title"],
            serp_snippet=row["serp_snippet"],
        )
        relevant = row["relevant"].strip().lower() == "yes"
        if relevant:
            tp += int(passes(result, settings.heuristic_threshold))
            fn += int(not passes(result, settings.heuristic_threshold))

    recall = tp / (tp + fn) if (tp + fn) else 0.0
    assert recall >= 0.90, (
        f"heuristic recall {recall:.2f} at threshold "
        f"{settings.heuristic_threshold:g} — run `python scripts/eval_scoring.py sweep`"
    )


def test_rescoring_heuristics_does_not_discard_paid_llm_work(pipe, repos, monkeypatch):
    """`score --rescore-all` must re-blend, not reset verdicts to pending_llm."""
    ids = seed_pending(pipe, repos, 1)
    client = FakeClient([reply(
        {"candidate_id": ids[0], "is_person_profile": True, "is_current_employee": True,
         "company_match": "exact", "role_match": "exact", "seniority": "mid",
         "score": 95, "confidence": 0.9, "reasons": ["exact title match"], "red_flags": []},
    )])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda settings, **kw: __import__("pitwall.scoring.llm", fromlist=["LLMScorer"]).LLMScorer(
            settings, client=client
        ),
    )
    pipe.score_llm(RunParams(use_llm=True))
    before = repos.scores.for_candidate(ids[0])[0]
    assert before["llm_score"] == 95

    stats = pipe.score_heuristics(RunParams(rescore_all=True, use_llm=False))
    after = repos.scores.for_candidate(ids[0])[0]

    assert stats.heuristic_scored >= 1
    assert after["llm_score"] == 95, "the LLM verdict survived the rescore"
    assert after["verdict"] != "pending_llm"
    assert after["company_match"] == "exact"
    assert "exact title match" in json.loads(after["reasons"])
    # the blend was recomputed against the fresh heuristic score
    assert after["final_score"] == pytest.approx(
        0.30 * after["heuristic_score"] + 0.70 * 95, abs=0.01
    )
