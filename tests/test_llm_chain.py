"""The LLM provider chain and its mid-run degradation — §0.4.

v1 shipped `LLM_FALLBACK=none`, so an unreachable provider halted the pipeline
and left every remaining candidate `pending_llm` with no explanation. v2 walks
`groq → zai → deepseek → ollama`, per batch, and records which provider produced
each verdict so a mixed-provider run can be audited afterwards.

No network: every scorer is built with an injected fake client.
"""

from __future__ import annotations

import json

import pytest

from pitwall.config import Settings
from pitwall.db import SCHEMA_VERSION, init_schema, open_repos
from pitwall.inputs import sync_to_db
from pitwall.pipeline import Pipeline, RunParams
from pitwall.scoring.llm import LLMScorer, QuotaExhausted

from .test_llm_contract import GOOD, FakeClient  # noqa: TID252
from .test_scoring_pipeline import seed_pending  # noqa: TID252


@pytest.fixture()
def pipe(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    return Pipeline(settings, repos)


def scorer_factory(clients: dict[str, FakeClient]):
    """Stand in for `pitwall.pipeline.LLMScorer`, one fake client per provider.

    A provider with no entry gets no client, so the real "is this configured?"
    check still runs for it — which is what the keyless-skip tests need.
    """

    def build(settings, *, provider=None, **kw):
        name = provider or settings.llm_provider
        return LLMScorer(settings, provider=name, client=clients.get(name))

    return build


# ───────────────────────── the chain, as configured ──────────────────────


def test_the_shipped_chain_is_groq_zai_deepseek_ollama():
    assert Settings(_env_file=None).llm_chain() == ["groq", "zai", "deepseek", "ollama"]


def test_ollama_is_last_not_first():
    """Local CPU inference is the offline safety net, not the working path."""
    chain = Settings(_env_file=None).llm_chain()
    assert chain[-1] == "ollama"
    assert chain[0] != "ollama"


def test_a_legacy_env_still_works():
    """LLM_PROVIDER / LLM_FALLBACK predate the chain and must keep resolving."""
    s = Settings(_env_file=None, llm_provider="ollama", llm_fallback="none")
    assert s.llm_chain()[0] == "ollama"
    assert "none" not in s.llm_chain()


def test_the_chain_deduplicates_keeping_the_first_mention():
    s = Settings(_env_file=None, llm_provider="zai", llm_provider_chain="zai,groq,zai")
    assert s.llm_chain() == ["zai", "groq", "ollama"]   # "ollama" is llm_fallback


def test_an_unknown_provider_is_rejected_at_config_time():
    with pytest.raises(ValueError, match="unknown LLM provider"):
        Settings(_env_file=None, llm_provider_chain="groq,gpt5")


def test_keyless_cloud_providers_are_dropped_but_ollama_is_not():
    s = Settings(_env_file=None, groq_api_key="gsk_x")
    assert s.usable_llm_chain() == ["groq", "ollama"]
    assert s.llm_has_key("ollama") is True
    assert s.llm_has_key("zai") is False


# ─────────────────────── opening the first usable one ────────────────────


def test_a_keyless_provider_is_skipped_silently(pipe, repos, monkeypatch):
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.llm_provider = "groq"
    pipe.settings.zai_api_key = "sk-zai"      # groq has none, zai does
    clients = {"zai": FakeClient([json.dumps(GOOD)])}
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 1
    assert any("groq skipped" in n and "API key" in n for n in stats.notes)


def test_the_stage_says_so_when_the_whole_chain_is_unusable(pipe, repos):
    pipe.settings.llm_provider_chain = ["groq", "deepseek"]
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_fallback = "none"
    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_calls == 0
    assert any("no usable LLM provider" in n for n in stats.notes)
    # v1's real failure: nobody may be dropped when the LLM cannot run.
    assert len(repos.scores.awaiting_llm(pipe.settings.prompt_version)) == 1


# ─────────────────────── degradation, mid-run, per batch ─────────────────


def test_a_dead_primary_falls_through_to_the_next_provider(pipe, repos, monkeypatch):
    """The §0 acceptance criterion: kill the primary mid-run, the run continues."""
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    clients = {
        "groq": FakeClient([ConnectionError("connection refused")]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 1
    assert any("degraded to zai" in n for n in stats.notes)
    assert len(clients["groq"].calls) == 1, "the dead provider is tried once, then dropped"


def test_a_quota_refusal_degrades_too(pipe, repos, monkeypatch):
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    clients = {
        "groq": FakeClient([RuntimeError("Error code: 429 - rate limit exceeded")]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_scored == 1
    assert any("degraded to zai" in n for n in stats.notes)


def test_a_badly_behaved_model_does_not_burn_the_chain(pipe, repos, monkeypatch):
    """Unparseable JSON is the repair ladder's job, not the chain's.

    Degrading over it would spend every provider on a problem the next one is
    just as likely to have, and would cost the `needs_review` record that keeps
    the person visible.
    """
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    clients = {
        "groq": FakeClient(["not json", "still not json"]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_needs_review == 1
    assert clients["zai"].calls == [], "zai was never reached"
    assert not any("degraded" in n for n in stats.notes)


def test_degradation_is_logged_on_both_sides(pipe, repos, monkeypatch, caplog):
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    clients = {
        "groq": FakeClient([ConnectionError("boom")]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))
    seed_pending(pipe, repos, 1)
    with caplog.at_level("WARNING"):
        pipe.score_llm(RunParams(use_llm=True))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "groq" in logged and "zai" in logged


def test_the_chain_running_out_leaves_everyone_pending_not_lost(pipe, repos, monkeypatch):
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq"]
    pipe.settings.llm_fallback = "none"
    pipe.settings.groq_api_key = "gsk_x"
    clients = {"groq": FakeClient([QuotaExhausted("out of credits")])}
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 2)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 0
    assert any("every LLM provider in the chain failed" in n for n in stats.notes)
    assert len(repos.scores.awaiting_llm(pipe.settings.prompt_version)) == 2


def test_a_spent_daily_cap_moves_down_the_chain(pipe, repos, monkeypatch):
    """The cap is per provider, so it is a reason to move on, not to stop."""
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    repos.quota.consume("llm:groq", pipe.settings.daily_llm_request_cap)
    clients = {"groq": FakeClient([]), "zai": FakeClient([json.dumps(GOOD)])}
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 1
    assert clients["groq"].calls == []
    assert any("cap" in n for n in stats.notes)


# ────────────────── the provider column, for auditability ────────────────


def test_the_scores_table_records_which_provider_answered(repos):
    assert init_schema(repos.conn) == SCHEMA_VERSION
    cols = {r["name"] for r in repos.conn.execute("PRAGMA table_info(scores)")}
    assert "provider" in cols


def test_a_v2_database_gains_the_provider_column_without_losing_data(tmp_path, settings):
    """Migration 2 -> 3 on a database that already has rows in it."""
    import sqlite3

    from pitwall import db as dbmod

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # the v2 shape: same DDL, minus the column and minus the version row
    conn.executescript(dbmod.DDL.replace(
        "    provider            TEXT,      "
        "-- which LLM answered, so mixed-provider runs audit\n", ""
    ))
    conn.execute("INSERT INTO schema_version(version) VALUES (2)")
    conn.execute(
        "INSERT INTO candidates (linkedin_slug, profile_url, full_name)"
        " VALUES ('a-person', 'https://www.linkedin.com/in/a-person', 'A Person')"
    )
    conn.execute(
        "INSERT INTO scores (candidate_id, heuristic_score, final_score, verdict,"
        " prompt_version) VALUES (1, 42.0, 42.0, 'pending_llm', 'v1')"
    )
    conn.commit()
    assert "provider" not in {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    conn.close()

    repos = open_repos(settings, path=path)
    cols = {r["name"] for r in repos.conn.execute("PRAGMA table_info(scores)")}
    assert "provider" in cols
    row = repos.conn.execute("SELECT * FROM scores").fetchone()
    assert row["heuristic_score"] == 42.0 and row["provider"] is None
    assert repos.conn.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"] == 1
    dbmod.close_all()


def test_the_answering_provider_is_recorded_on_every_verdict(pipe, repos, monkeypatch):
    pipe.settings.llm_provider = "zai"
    pipe.settings.zai_api_key = "sk-zai"
    clients = {"zai": FakeClient([json.dumps(GOOD)])}
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    ids = seed_pending(pipe, repos, 1)
    pipe.score_llm(RunParams(use_llm=True))
    row = repos.scores.for_candidate(ids[0])[0]
    assert row["provider"] == "zai"
    assert row["model"] == pipe.settings.zai_model


def test_a_mixed_provider_run_is_auditable_afterwards(pipe, repos, monkeypatch):
    """The point of the column: two providers in one run, both attributable."""
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    pipe.settings.llm_batch_size = 1

    first = json.dumps({"results": [{**GOOD["results"][0], "candidate_id": 1}]})
    second = json.dumps({"results": [{**GOOD["results"][0], "candidate_id": 2}]})
    clients = {
        "groq": FakeClient([first, ConnectionError("dropped")]),
        "zai": FakeClient([second]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 2)
    pipe.score_llm(RunParams(use_llm=True))

    providers = {
        r["candidate_id"]: r["provider"]
        for r in repos.conn.execute("SELECT candidate_id, provider FROM scores")
    }
    assert set(providers.values()) == {"groq", "zai"}


# ──────────── a pause is not an exhaustion (§0.4, learned the hard way) ─────


def test_a_rate_limit_waits_and_retries_the_same_provider(pipe, repos, monkeypatch):
    """The first real Groq run degraded to a local CPU model over a per-minute
    token ceiling that would have cleared in ten seconds. It must not."""
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    pipe.settings.llm_rate_limit_max_wait = 0.01

    clients = {
        "groq": FakeClient([
            RuntimeError("429 rate limit reached on tokens per minute (TPM). "
                         "Please try again in 0.01s"),
            json.dumps(GOOD),
        ]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 1
    assert len(clients["groq"].calls) == 2, "the same provider was asked again"
    assert clients["zai"].calls == [], "the chain was never walked"
    assert not any("degraded" in n for n in stats.notes)


def test_a_persistent_rate_limit_eventually_degrades(pipe, repos, monkeypatch):
    """Waiting for ever is not resilience."""
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    pipe.settings.llm_rate_limit_retries = 2
    pipe.settings.llm_rate_limit_max_wait = 0.01

    limit = RuntimeError("429 rate limit reached (TPM). Please try again in 0.01s")
    clients = {
        "groq": FakeClient([limit, limit, limit, limit]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))

    assert stats.llm_scored == 1
    assert any("rate limited" in n and "unusable" in n for n in stats.notes)
    assert any("degraded to zai" in n for n in stats.notes)


def test_a_daily_ceiling_degrades_immediately(pipe, repos, monkeypatch):
    """Per-day is not per-minute: no amount of waiting inside one run fixes it."""
    pipe.settings.llm_provider = "groq"
    pipe.settings.llm_provider_chain = ["groq", "zai"]
    pipe.settings.groq_api_key = "gsk_x"
    pipe.settings.zai_api_key = "sk-zai"
    clients = {
        "groq": FakeClient([RuntimeError(
            "429 rate limit reached on requests per day (RPD): Limit 1000, Used 1000"
        )]),
        "zai": FakeClient([json.dumps(GOOD)]),
    }
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))

    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True))
    assert stats.llm_scored == 1
    assert len(clients["groq"].calls) == 1, "no point waiting out a daily limit"
    assert any("degraded to zai" in n for n in stats.notes)


def test_a_rate_limit_also_lowers_the_concurrency(pipe, repos, monkeypatch):
    pipe.settings.llm_provider = "zai"
    pipe.settings.zai_api_key = "sk-zai"
    pipe.settings.llm_rate_limit_max_wait = 0.01
    clients = {"zai": FakeClient([
        RuntimeError("429 tokens per minute. Please try again in 0.01s"),
        json.dumps(GOOD), json.dumps(GOOD),
    ])}
    monkeypatch.setattr("pitwall.pipeline.LLMScorer", scorer_factory(clients))
    seed_pending(pipe, repos, 1)
    stats = pipe.score_llm(RunParams(use_llm=True, concurrency=4))
    assert any("concurrency reduced" in n for n in stats.notes)


def test_groqs_own_rate_limit_message_is_not_read_as_a_dead_account():
    """Groq ends every 429 with an upgrade link containing the word `billing`.

    A substring match on that read a ten-second pause as an exhausted account,
    twice, on the first real run — and sent 300 candidates to a local CPU model.
    """
    from pitwall.scoring.llm import classify_error, retry_after_seconds

    class Err(RuntimeError):
        status_code = 429

    tpm = Err(
        "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
        "`openai/gpt-oss-20b` in organization `org_x` service tier `on_demand` on "
        "tokens per minute (TPM): Limit 8000, Used 7863, Requested 1421. Please try "
        "again in 9.622s. Need more tokens? Upgrade to Dev Tier today at "
        "https://console.groq.com/settings/billing', 'code': 'rate_limit_exceeded'}}"
    )
    assert classify_error(tpm) == "rate_limit"
    assert retry_after_seconds(tpm) == pytest.approx(9.622)

    rpd = Err(
        "Error code: 429 - Rate limit reached on requests per day (RPD): Limit 1000, "
        "Used 1000. Upgrade at https://console.groq.com/settings/billing"
    )
    assert classify_error(rpd) == "quota", "per-day is terminal even with a billing link"
