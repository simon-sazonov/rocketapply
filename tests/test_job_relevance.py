"""CV relevance scoring for postings — §2.6.

Optional by design and built last, because §2.6 says to ship §2.5 first and see
whether it is needed: sorting by "companies where I know people" may be signal
enough on its own. So the first thing this file asserts is that it does nothing
until the owner writes a profile.

It reuses the existing LLM layer rather than growing a second one — same client,
same batching, same repair ladder, same provider chain — so the tests here are
about the contract and the keying, not about HTTP.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from pitwall.config import Settings
from pitwall.models import JobRelevance
from pitwall.pipeline import Pipeline, RunParams, RunStats
from pitwall.scoring.jobs import (
    JobRelevanceScorer,
    job_payload,
    load_profile,
    parse_relevance,
    profile_path,
)
from pitwall.scoring.schema import STRICT_JOB_BATCH_SCHEMA, job_response_format
from pitwall.sources.jobs import JobPosting

from .conftest import FIXTURES  # noqa: TID252

ROOT = FIXTURES.parents[1]

GOOD = {
    "results": [
        {"job_id": 1, "score": 88, "confidence": 0.8, "seniority_fit": "match",
         "reasons": ["Posting asks for Monte Carlo race simulation, which the profile names"],
         "red_flags": []},
        {"job_id": 2, "score": 12, "confidence": 0.9, "seniority_fit": "above",
         "reasons": ["Head of Department; the profile targets a junior role"],
         "red_flags": ["requires 10 years' experience"]},
    ]
}


class FakeClient:
    def __init__(self, replies=None):
        self.replies = list(replies or [json.dumps(GOOD)])
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=180),
        )


PROFILE = "# Target role\nGraduate race strategy engineer.\n\n# Skills\nPython, Monte Carlo."


@pytest.fixture()
def with_profile(project):
    (project / "config" / "profile.md").write_text(PROFILE, encoding="utf-8")
    return project


def jobs_payload():
    return [
        {"id": 1, "title": "Race Strategy Engineer", "company": "Apex",
         "location": "Vicenza", "employment_type": "Full-time",
         "description": "Monte Carlo race simulation and in-race calls."},
        {"id": 2, "title": "Head of Vehicle Dynamics", "company": "Apex",
         "location": "Vicenza", "employment_type": "Full-time",
         "description": "Lead a department of 20."},
    ]


# ──────────────────────── off until there is a profile ───────────────────


def test_the_shipped_profile_is_a_template_and_reads_as_absent(project):
    """Nothing runs until the owner replaces the template — §2.6 is optional.

    Isolated to the throwaway project root, like every other test here.
    It used to read the real `config/`, so it asserted the owner had NOT written
    `config/my_profile.md` — which `profile_path()` prefers when it exists, and
    which is exactly what the template asks them to do. Writing one turned this
    test permanently red while the behaviour it describes was still correct.
    """
    shipped = ROOT / "config" / "profile.md"
    assert shipped.exists(), "the template ships so there is something to edit"
    (project / "config" / "profile.md").write_text(
        shipped.read_text(encoding="utf-8"), encoding="utf-8")
    assert profile_path().exists()
    assert load_profile() is None


def test_a_written_profile_is_picked_up(with_profile):
    assert load_profile() == PROFILE


def test_an_empty_profile_reads_as_absent(project):
    (project / "config" / "profile.md").write_text("   \n", encoding="utf-8")
    assert load_profile() is None


def test_the_stage_does_nothing_without_a_profile(repos, settings, company, role):
    from pitwall.inputs import sync_to_db

    sync_to_db(repos.conn, [company], [role])
    stats = Pipeline(settings, repos).score_jobs(RunParams(), RunStats())
    assert stats.jobs_scored == 0
    assert any("config/profile.md" in n for n in stats.notes)


# ────────────────────────────── the contract ─────────────────────────────


def test_a_valid_reply_is_parsed():
    batch = parse_relevance(json.dumps(GOOD))
    assert [v.job_id for v in batch.results] == [1, 2]
    assert batch.results[0].score == 88


def test_prose_wrapped_json_is_recovered():
    assert parse_relevance("Sure!\n```json\n" + json.dumps(GOOD) + "\n```").results


def test_a_bare_single_object_is_wrapped():
    batch = parse_relevance(json.dumps(GOOD["results"][0]))
    assert len(batch.results) == 1


def test_unparseable_text_is_none_rather_than_a_guess():
    assert parse_relevance("no.") is None


def test_out_of_range_values_are_clamped():
    v = JobRelevance(job_id=1, score=999, confidence=85, seniority_fit="MATCH")
    assert v.score == 100.0 and v.confidence == 0.85 and v.seniority_fit == "match"


def test_an_unknown_seniority_value_does_not_fail_the_batch():
    assert JobRelevance(job_id=1, seniority_fit="perfect").seniority_fit == "unknown"


def test_the_strict_schema_follows_the_constrained_decoding_rules():
    item = STRICT_JOB_BATCH_SCHEMA["properties"]["results"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    assert "minimum" not in item["properties"]["score"]


# ─────────────────────────────── the scorer ──────────────────────────────


def test_the_scorer_uses_the_job_prompt_not_the_candidate_one(settings):
    scorer = JobRelevanceScorer(settings, client=FakeClient())
    assert "job-matching" in scorer.system_prompt
    assert "recruitment-research classifier" not in scorer.system_prompt


def test_the_profile_and_the_postings_both_reach_the_model(settings):
    client = FakeClient()
    JobRelevanceScorer(settings, client=client).score_jobs(PROFILE, jobs_payload())
    prompt = client.calls[0]["messages"][1]["content"]
    assert "Monte Carlo" in prompt          # from the profile
    assert "Race Strategy Engineer" in prompt   # from the posting
    assert "job_id: 1" in prompt


def test_a_strict_model_gets_the_job_schema_not_the_candidate_schema(settings):
    settings.llm_model = "openai/gpt-oss-20b"
    client = FakeClient()
    JobRelevanceScorer(settings, client=client).score_jobs(PROFILE, jobs_payload())
    rf = client.calls[0]["response_format"]
    assert rf["json_schema"]["name"] == "pitwall_job_relevance"


def test_a_non_strict_model_falls_back_to_json_mode(settings):
    client = FakeClient()
    JobRelevanceScorer(settings, client=client).score_jobs(PROFILE, jobs_payload())
    assert client.calls[0]["response_format"] == job_response_format(False)


def test_temperature_zero_survives(settings):
    client = FakeClient()
    JobRelevanceScorer(settings, client=client).score_jobs(PROFILE, jobs_payload())
    assert client.calls[0]["temperature"] == 0


def test_verdicts_come_back_in_the_order_asked(settings):
    out = JobRelevanceScorer(settings, client=FakeClient()).score_jobs(PROFILE, jobs_payload())
    assert [v.job_id for v in out.verdicts] == [1, 2]
    assert out.unscored == []


def test_an_omitted_posting_is_reported_not_dropped(settings):
    reply = json.dumps({"results": [GOOD["results"][0]]})
    out = JobRelevanceScorer(settings, client=FakeClient([reply])).score_jobs(
        PROFILE, jobs_payload()
    )
    assert [v.job_id for v in out.verdicts] == [1]
    assert out.unscored == [2], "it keeps its NULL score and comes back next run"


def test_a_failed_call_marks_the_whole_batch_unscored(settings):
    out = JobRelevanceScorer(
        settings, client=FakeClient([RuntimeError("boom")])
    ).score_jobs(PROFILE, jobs_payload())
    assert out.verdicts == []
    assert out.unscored == [1, 2]
    assert out.provider_error is True


def test_an_empty_batch_short_circuits(settings):
    client = FakeClient()
    assert JobRelevanceScorer(settings, client=client).score_jobs(PROFILE, []).verdicts == []
    assert client.calls == []


def test_token_usage_is_accounted(settings):
    scorer = JobRelevanceScorer(settings, client=FakeClient())
    scorer.score_jobs(PROFILE, jobs_payload())
    assert scorer.usage.tokens_in == 900


def test_the_payload_is_trimmed_to_what_the_model_needs():
    row = {"id": 7, "title": "T", "company_name": "C", "location": "L",
           "employment_type": "F", "description": "x" * 9000, "raw_json": "…"}
    payload = job_payload(row)
    assert set(payload) == {"id", "title", "company", "location", "employment_type",
                            "description"}
    assert len(payload["description"]) == 3000
    assert payload["company"] == "C"


# ──────────────────── keyed by prompt version, additively ────────────────


def test_scores_are_keyed_by_their_own_prompt_version(repos, settings, with_profile,
                                                      company, role, monkeypatch):
    from pitwall.inputs import sync_to_db

    sync_to_db(repos.conn, [company], [role])
    settings.zai_api_key = "sk-test"
    settings.llm_batch_size = 8
    source_id = repos.job_sources.upsert("s", "ats_json")
    for i in (1, 2):
        repos.jobs.upsert(source_id, JobPosting(title=f"J{i}", url=f"https://x.test/{i}"))

    monkeypatch.setattr(
        "pitwall.scoring.jobs.JobRelevanceScorer",
        lambda s, **kw: JobRelevanceScorer(s, provider="zai", client=FakeClient()),
    )
    pipe = Pipeline(settings, repos)
    stats = pipe.score_jobs(RunParams(), RunStats())
    assert stats.jobs_scored == 2

    rows = {r["id"]: r for r in repos.conn.execute("SELECT * FROM jobs")}
    assert rows[1]["relevance_score"] == 88
    assert rows[1]["relevance_prompt_version"] == "v1"
    assert "Monte Carlo" in rows[1]["relevance_reasons"]
    assert "⚠" in rows[2]["relevance_reasons"], "red flags are kept, marked"

    # already scored under v1: not asked again
    assert repos.jobs.awaiting_relevance("v1") == []
    # a new rubric version is additive — everything is pending again under v2
    assert len(repos.jobs.awaiting_relevance("v2")) == 2


def test_the_two_prompt_versions_are_independent():
    """Changing the candidate rubric must not invalidate every job score."""
    settings = Settings(_env_file=None)
    assert settings.job_prompt_version == "v1"
    assert settings.prompt_version == "v1"
    shipped = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert shipped["llm"]["job_prompt_version"] == "v1"
    assert shipped["llm"]["prompt_version"] == "v1"

    # and they really are separate fields, not an alias for one another
    other = Settings(_env_file=None, job_prompt_version="v2")
    assert other.prompt_version == "v1" and other.job_prompt_version == "v2"


def test_a_handover_mid_job_scoring_keeps_a_job_scorer(repos, settings, with_profile,
                                                       company, role, monkeypatch):
    """`_degrade` rebuilds from `_llm_factory`.

    Before that, degradation inside the job stage produced a plain LLMScorer and
    the next line called `.score_jobs` on it — an AttributeError that only the
    type checker saw, because no test had made a provider fail here.
    """
    from pitwall.inputs import sync_to_db

    sync_to_db(repos.conn, [company], [role])
    settings.llm_provider = "zai"
    settings.llm_provider_chain = ["zai", "deepseek"]
    settings.zai_api_key = "sk-zai"
    settings.deepseek_api_key = "sk-deep"
    settings.llm_batch_size = 1

    source_id = repos.job_sources.upsert("s", "ats_json")
    for i in (1, 2):
        repos.jobs.upsert(source_id, JobPosting(title=f"J{i}", url=f"https://x.test/{i}"))

    clients = {
        "zai": FakeClient([json.dumps(GOOD), RuntimeError("connection reset")]),
        "deepseek": FakeClient([json.dumps(GOOD)]),
    }

    def factory(s, *, provider=None, **kw):
        name = provider or s.llm_provider
        return JobRelevanceScorer(s, provider=name, client=clients.get(name))

    monkeypatch.setattr("pitwall.scoring.jobs.JobRelevanceScorer", factory)

    pipe = Pipeline(settings, repos)
    stats = pipe.score_jobs(RunParams(), RunStats())

    assert stats.jobs_scored == 2, "the second batch survived the handover"
    assert any("degraded to deepseek" in n for n in stats.notes)


def test_a_spent_chain_during_scoring_still_banks_its_usage(repos, settings,
                                                            company, role, monkeypatch):
    """`_finish_llm` was handed None when the chain ran out mid-wave."""
    from pitwall.inputs import sync_to_db
    from pitwall.scoring.llm import LLMScorer

    sync_to_db(repos.conn, [company], [role])
    settings.llm_provider = "zai"
    settings.llm_provider_chain = ["zai"]
    settings.llm_fallback = "none"
    settings.zai_api_key = "sk-zai"
    settings.llm_rate_limit_retries = 0
    settings.llm_rate_limit_max_wait = 0.01

    limit = RuntimeError("429 tokens per minute. Please try again in 0.01s")
    client = FakeClient([limit, limit, limit, limit])
    monkeypatch.setattr(
        "pitwall.pipeline.LLMScorer",
        lambda s, **kw: LLMScorer(s, provider="zai", client=client),
    )
    from .test_scoring_pipeline import seed_pending

    pipe = Pipeline(settings, repos)
    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))   # must not raise

    assert stats.llm_scored == 0
    assert len(repos.scores.awaiting_llm(settings.prompt_version)) == 1
