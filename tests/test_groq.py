"""The Groq adapter, against recorded responses — §0.3 acceptance criteria.

No network anywhere: the chat completion is replayed from
`tests/fixtures/groq_completion.json` through a fake client, and the model
catalogue from `tests/fixtures/groq_models.json` through a patched `httpx.get`.

The two behaviours worth pinning down:

* **the model is configuration, never a constant.** Groq retires model IDs on
  short notice, so `LLM_MODEL` overrides whatever the provider table says and
  `scripts/setup_groq.py` checks it against the live catalogue.
* **provider-specific parameters stay provider-specific.** Strict JSON and
  `reasoning_effort` are gated on the model, so zai, deepseek and ollama send
  byte-identical requests to the ones v1 sent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from pitwall.config import Settings
from pitwall.scoring.llm import (
    STRICT_JSON_MODELS,
    LLMScorer,
    LLMUnavailable,
    list_models,
)
from pitwall.scoring.schema import STRICT_BATCH_SCHEMA, response_format

from .conftest import FIXTURES  # noqa: TID252

MODELS = json.loads((FIXTURES / "groq_models.json").read_text(encoding="utf-8"))
COMPLETION = json.loads((FIXTURES / "groq_completion.json").read_text(encoding="utf-8"))
REPLY = COMPLETION["choices"][0]["message"]["content"]


class FakeClient:
    """Replays a recorded Groq completion and records the request."""

    def __init__(self, replies=None) -> None:
        self.replies = list(replies or [REPLY])
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        usage = COMPLETION["usage"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))],
            usage=SimpleNamespace(
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
            ),
        )


@pytest.fixture()
def groq_settings(settings):
    settings.llm_provider = "groq"
    settings.groq_api_key = "gsk_test"
    return settings


def payload(*ids):
    return [
        {"id": i, "raw_title": f"P{i} - Data Scientist - McLaren Racing",
         "raw_snippet": "Woking", "profile_url": f"https://www.linkedin.com/in/p{i}"}
        for i in ids
    ]


# ─────────────────────────── config and defaults ─────────────────────────


def test_groq_is_the_shipped_default_provider():
    assert Settings(_env_file=None).llm_provider == "groq"


def test_the_groq_endpoint_is_openai_compatible(groq_settings):
    cfg = groq_settings.llm_config("groq")
    assert cfg["base_url"] == "https://api.groq.com/openai/v1"
    assert cfg["api_key"] == "gsk_test"


def test_the_default_model_is_one_the_catalogue_fixture_carries():
    """A tripwire for the hardcoded default drifting out of the catalogue."""
    default = Settings(_env_file=None).groq_model
    assert default in {row["id"] for row in MODELS["data"]}
    assert default in STRICT_JSON_MODELS, "prefer a model that can be schema-constrained"


def test_llm_model_overrides_the_provider_default(groq_settings):
    """Groq deprecates ids; the model has to be settable without a code change."""
    groq_settings.llm_model = "qwen/qwen3.6-27b"
    assert groq_settings.llm_config("groq")["model"] == "qwen/qwen3.6-27b"
    assert LLMScorer(groq_settings, client=FakeClient()).model == "qwen/qwen3.6-27b"


def test_the_override_applies_to_every_provider(groq_settings):
    groq_settings.llm_model = "custom-thing"
    assert groq_settings.llm_config("ollama")["model"] == "custom-thing"


def test_groq_is_priced_at_zero_so_the_estimate_stays_honest(groq_settings):
    assert groq_settings.llm_prices["groq"] == {"input": 0.0, "output": 0.0}


# ────────────────────────── the catalogue call ───────────────────────────


def test_list_models_reads_the_openai_envelope(groq_settings, monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: MODELS, text="")

    monkeypatch.setattr(httpx, "get", fake_get)
    models = list_models(groq_settings, "groq")

    assert captured["url"] == "https://api.groq.com/openai/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer gsk_test"
    assert "openai/gpt-oss-20b" in models
    assert models == sorted(models), "sorted so the print-out is stable"


def test_list_models_refuses_without_a_key(groq_settings):
    groq_settings.groq_api_key = ""
    with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
        list_models(groq_settings, "groq")


def test_a_rejected_key_says_so_rather_than_looking_like_an_outage(
    groq_settings, monkeypatch
):
    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: SimpleNamespace(status_code=401, json=dict, text="Invalid API Key"),
    )
    with pytest.raises(LLMUnavailable, match="rejected the API key"):
        list_models(groq_settings, "groq")


def test_an_unreachable_host_is_reported_as_unavailable(groq_settings, monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(LLMUnavailable, match="cannot reach"):
        list_models(groq_settings, "groq")


# ─────────────────────── strict structured output ────────────────────────


def test_a_strict_model_gets_a_schema_constrained_request(groq_settings):
    client = FakeClient()
    scorer = LLMScorer(groq_settings, client=client)
    assert scorer.supports_strict_json() is True
    scorer.score_batch(payload(1, 2), None, None)

    rf = client.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == STRICT_BATCH_SCHEMA


def test_a_non_strict_model_falls_back_to_plain_json_mode(groq_settings):
    groq_settings.llm_model = "groq/compound"
    client = FakeClient()
    scorer = LLMScorer(groq_settings, client=client)
    assert scorer.supports_strict_json() is False
    scorer.score_batch(payload(1), None, None)
    assert client.calls[0]["response_format"] == {"type": "json_object"}


def test_the_strict_schema_satisfies_the_constrained_decoding_rules():
    """Every property required, additionalProperties false, no value assertions."""
    item = STRICT_BATCH_SCHEMA["properties"]["results"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    assert STRICT_BATCH_SCHEMA["additionalProperties"] is False
    assert "minimum" not in item["properties"]["score"]
    assert "maxItems" not in item["properties"]["reasons"]


def test_the_strict_schema_still_carries_the_enums():
    """Dropping the numeric bounds must not drop the vocabulary."""
    props = STRICT_BATCH_SCHEMA["properties"]["results"]["items"]["properties"]
    assert "same_name_different_org" in props["company_match"]["enum"]
    assert props["role_match"]["enum"] == ["exact", "adjacent", "unrelated", "unknown"]


def test_other_providers_are_untouched_by_the_strict_path(settings):
    """zai/deepseek/ollama must send exactly what v1 sent."""
    for provider in ("zai", "deepseek", "ollama"):
        client = FakeClient()
        LLMScorer(settings, provider=provider, client=client).score_batch(
            payload(1), None, None
        )
        call = client.calls[0]
        assert call["response_format"] == response_format(strict=False)
        assert "reasoning_effort" not in call
        assert "extra_body" not in call


# ────────────────────────── reasoning parameters ─────────────────────────


def test_a_reasoning_model_hides_its_trace_and_keeps_the_effort_low(groq_settings):
    client = FakeClient()
    scorer = LLMScorer(groq_settings, client=client)
    assert scorer.is_reasoning_model() is True
    scorer.score_batch(payload(1), None, None)

    call = client.calls[0]
    assert call["reasoning_effort"] == "low"
    # raw + JSON mode is a 400 on Groq, and the trace is billed as output.
    assert call["extra_body"] == {"reasoning_format": "hidden"}


def test_the_reasoning_effort_is_configurable(groq_settings):
    groq_settings.llm_reasoning_effort = "medium"
    client = FakeClient()
    LLMScorer(groq_settings, client=client).score_batch(payload(1), None, None)
    assert client.calls[0]["reasoning_effort"] == "medium"


def test_a_non_reasoning_model_sends_no_reasoning_parameters(groq_settings):
    groq_settings.llm_model = "groq/compound"
    client = FakeClient()
    scorer = LLMScorer(groq_settings, client=client)
    assert scorer.is_reasoning_model() is False
    scorer.score_batch(payload(1), None, None)
    assert "reasoning_effort" not in client.calls[0]


# ──────────────────────────── the contract ───────────────────────────────


def test_a_recorded_groq_reply_produces_usable_verdicts(groq_settings):
    scorer = LLMScorer(groq_settings, client=FakeClient())
    out = scorer.score_batch(payload(1, 2), None, None)
    assert [v.candidate_id for v in out.verdicts] == [1, 2]
    assert out.needs_review == []
    assert out.provider == "groq"


def test_the_wrong_org_trap_survives_the_round_trip(groq_settings):
    """The one verdict that proves the rubric reached the model."""
    out = LLMScorer(groq_settings, client=FakeClient()).score_batch(payload(1, 2), None, None)
    trap = next(v for v in out.verdicts if v.candidate_id == 2)
    assert trap.company_match == "same_name_different_org"
    assert trap.score < 40


def test_temperature_zero_survives_the_groq_path(groq_settings):
    client = FakeClient()
    LLMScorer(groq_settings, client=client).score_batch(payload(1), None, None)
    assert client.calls[0]["temperature"] == 0
    assert client.calls[0]["top_p"] == 1


def test_token_usage_is_accounted_from_the_recorded_response(groq_settings):
    scorer = LLMScorer(groq_settings, client=FakeClient())
    scorer.score_batch(payload(1, 2), None, None)
    assert scorer.usage.tokens_in == COMPLETION["usage"]["prompt_tokens"]
    assert scorer.usage.tokens_out == COMPLETION["usage"]["completion_tokens"]
    assert scorer.estimated_cost() == 0.0  # free tier


def test_a_missing_key_names_the_env_var(groq_settings):
    groq_settings.groq_api_key = ""
    with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
        _ = LLMScorer(groq_settings).client
