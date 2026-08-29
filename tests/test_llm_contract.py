"""The LLM contract: good JSON, prose-wrapped JSON, malformed JSON, 429 (§12.1).

Mocked client only — this file never touches a network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pitwall.models import LLMVerdict
from pitwall.scoring.llm import (
    LLMRateLimited,
    LLMScorer,
    LLMUnavailable,
    QuotaExhausted,
    batched,
    parse_batch,
    render_user_prompt,
    repair_json,
)
from pitwall.scoring.schema import BATCH_SCHEMA, SCHEMA_HINT, response_format

GOOD = {
    "results": [
        {
            "candidate_id": 1,
            "is_person_profile": True,
            "full_name": "Jane Doe",
            "current_company": "McLaren Racing",
            "current_title": "Senior Data Scientist",
            "is_current_employee": True,
            "company_match": "exact",
            "role_match": "exact",
            "seniority": "senior",
            "motorsport_experience": True,
            "score": 92,
            "confidence": 0.86,
            "reasons": ["Title states 'Senior Data Scientist - McLaren Racing'"],
            "red_flags": [],
        }
    ]
}


class FakeClient:
    """Minimal stand-in for the OpenAI client."""

    def __init__(self, replies, usage=(1200, 300)):
        self.replies = list(replies)
        self.calls = []
        self.usage = usage
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=self.usage[0], completion_tokens=self.usage[1]),
        )


def payload(*ids):
    return [
        {"id": i, "raw_title": f"P{i} - Data Scientist - McLaren Racing",
         "raw_snippet": "London", "profile_url": f"https://www.linkedin.com/in/p{i}"}
        for i in ids
    ]


# ─────────────────────────── the repair ladder ───────────────────────────


def test_plain_json_parses():
    assert repair_json(json.dumps(GOOD))["results"][0]["candidate_id"] == 1


def test_markdown_fenced_json_parses():
    assert repair_json("```json\n" + json.dumps(GOOD) + "\n```") is not None


def test_prose_wrapped_json_is_recovered():
    text = "Sure! Here are the results:\n" + json.dumps(GOOD) + "\nHope that helps."
    assert repair_json(text)["results"][0]["score"] == 92


def test_json_containing_braces_in_strings_is_recovered():
    payload_text = 'Note: {not json}. {"results": [{"candidate_id": 5, "reasons": ["a } b"]}]}'
    out = repair_json(payload_text)
    assert out["results"][0]["candidate_id"] == 5


def test_unparseable_text_returns_none():
    assert repair_json("no json at all") is None
    assert repair_json("") is None
    assert parse_batch("total nonsense") is None


def test_a_bare_single_object_is_wrapped():
    batch = parse_batch(json.dumps(GOOD["results"][0]))
    assert batch is not None and len(batch.results) == 1


# ──────────────────────────── schema coercion ────────────────────────────


def test_loose_types_are_coerced_to_the_schema():
    batch = parse_batch(
        '{"results":[{"candidate_id":3,"score":"88","confidence":86,'
        '"company_match":"Exact","role_match":"ADJACENT","seniority":"Mid",'
        '"reasons":"only one reason","red_flags":null}]}'
    )
    v = batch.results[0]
    assert v.score == 88.0
    assert v.confidence == pytest.approx(0.86)  # 0-100 confidence rescaled
    assert v.company_match == "exact"
    assert v.role_match == "adjacent"
    assert v.seniority == "mid"
    assert v.reasons == ["only one reason"]
    assert v.red_flags == []


def test_out_of_range_values_are_clamped():
    v = LLMVerdict(candidate_id=1, score=1000, confidence=-5)
    assert v.score == 100.0
    assert v.confidence == 0.0


def test_unknown_enum_values_fall_back_to_unknown_without_failing_the_batch():
    v = LLMVerdict(candidate_id=1, company_match="wibble", role_match=None, seniority="")
    assert v.company_match == "unknown"
    assert v.role_match == "unknown"
    assert v.seniority == "unknown"
    # ...and a whole batch survives one invented value
    batch = parse_batch(
        '{"results":[{"candidate_id":1,"company_match":"totally_made_up"},'
        '{"candidate_id":2,"company_match":"exact"}]}'
    )
    assert batch is not None
    assert [v.candidate_id for v in batch.results] == [1, 2]
    assert batch.results[0].company_match == "unknown"


def test_the_schema_advertises_the_same_name_different_org_verdict():
    props = BATCH_SCHEMA["properties"]["results"]["items"]["properties"]
    assert "same_name_different_org" in props["company_match"]["enum"]
    assert "same_name_different_org" in SCHEMA_HINT
    assert response_format(False) == {"type": "json_object"}
    assert response_format(True)["type"] == "json_schema"


# ─────────────────────────────── the scorer ──────────────────────────────


def test_valid_reply_is_scored(settings):
    client = FakeClient([json.dumps(GOOD)])
    scorer = LLMScorer(settings, client=client)
    out = scorer.score_batch(payload(1), None, None)
    assert [v.candidate_id for v in out.verdicts] == [1]
    assert out.needs_review == []
    assert scorer.usage.calls == 1
    assert scorer.usage.tokens_in == 1200


def test_deterministic_call_parameters(settings):
    client = FakeClient([json.dumps(GOOD)])
    LLMScorer(settings, client=client).score_batch(payload(1), None, None)
    call = client.calls[0]
    assert call["temperature"] == 0
    assert call["top_p"] == 1
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["role"] == "system"


def test_bad_json_triggers_exactly_one_corrective_retry(settings):
    client = FakeClient(["I refuse to answer in JSON.", json.dumps(GOOD)])
    out = LLMScorer(settings, client=client).score_batch(payload(1), None, None)
    assert len(client.calls) == 2
    assert "not valid JSON" in client.calls[1]["messages"][-1]["content"]
    assert [v.candidate_id for v in out.verdicts] == [1]


def test_persistently_bad_json_marks_needs_review_and_keeps_the_person(settings):
    client = FakeClient(["nope", "still nope"])
    out = LLMScorer(settings, client=client).score_batch(payload(1, 2), None, None)
    assert out.verdicts == []
    assert out.needs_review == [1, 2]          # nobody is silently dropped
    assert out.error


def test_omitted_candidates_are_marked_needs_review(settings):
    client = FakeClient([json.dumps(GOOD)])   # asked about 1 and 2, answered only 1
    out = LLMScorer(settings, client=client).score_batch(payload(1, 2), None, None)
    assert [v.candidate_id for v in out.verdicts] == [1]
    assert out.needs_review == [2]


def test_a_plain_429_is_a_pause_not_an_exhaustion(settings):
    """§0.4/§1.1: a per-minute ceiling clears in seconds.

    v1 mapped every 429 to QuotaExhausted, which meant the first real Groq run
    abandoned a 1,000 tok/s cloud model for a local CPU one over a limit that
    would have cleared in ten seconds.
    """
    err = RuntimeError("Error code: 429 - rate limit exceeded. Please try again in 9.6s")
    with pytest.raises(LLMRateLimited) as exc:
        LLMScorer(settings, client=FakeClient([err])).score_batch(payload(1), None, None)
    assert exc.value.retry_after == pytest.approx(9.6)


def test_a_terminal_429_still_raises_quota_exhausted(settings):
    """Out of daily allowance or out of credits: only a new provider helps."""
    for message in (
        "Error code: 429 - rate limit reached on requests per day (RPD): Limit 1000",
        "Error code: 429 - quota exceeded",
        "Error code: 402 - insufficient credits",
    ):
        with pytest.raises(QuotaExhausted):
            LLMScorer(settings, client=FakeClient([RuntimeError(message)])).score_batch(
                payload(1), None, None
            )


def test_an_ordinary_error_does_not_lose_the_batch(settings):
    client = FakeClient([RuntimeError("connection reset")])
    out = LLMScorer(settings, client=client).score_batch(payload(1, 2), None, None)
    assert out.needs_review == [1, 2]
    assert "connection reset" in out.error


def test_switching_provider_mid_run(settings):
    scorer = LLMScorer(settings, client=FakeClient([json.dumps(GOOD)]))
    assert scorer.provider == "zai"
    scorer.switch_to("ollama")
    assert scorer.provider == "ollama"
    assert scorer.degraded_from == "zai"
    assert scorer.model == settings.ollama_model


def test_cost_accounting_uses_the_provider_price_table(settings):
    scorer = LLMScorer(settings, provider="deepseek", client=FakeClient([json.dumps(GOOD)]))
    scorer.score_batch(payload(1), None, None)
    expected = 1200 / 1e6 * 0.14 + 300 / 1e6 * 0.28
    assert scorer.estimated_cost() == pytest.approx(expected)


def test_empty_batches_short_circuit(settings):
    client = FakeClient([])
    out = LLMScorer(settings, client=client).score_batch([], None, None)
    assert out.verdicts == [] and client.calls == []


# ─────────────────────────────── prompting ───────────────────────────────


def test_the_prompt_names_the_target_and_the_disambiguations(company, role):
    text = render_user_prompt(company, role, payload(1))
    assert "McLaren Racing" in text
    assert "NOT these organisations" in text
    assert "Health Care" in text
    assert "Data Scientist" in text
    assert "candidate_id: 1" in text
    assert "McLaren Health Care" in text          # the few-shot trap example
    assert "Ex-Scuderia Ferrari" in text          # the few-shot former-employee example


def test_the_prompt_carries_the_raw_evidence_not_our_parse(company, role):
    text = render_user_prompt(
        company, role,
        [{"id": 9, "raw_title": "RAW TITLE HERE", "raw_snippet": "RAW SNIPPET HERE",
          "profile_url": "https://www.linkedin.com/in/x"}],
    )
    assert "RAW TITLE HERE" in text and "RAW SNIPPET HERE" in text


def test_batching_respects_the_configured_size():
    assert [len(b) for b in batched(list(range(19)), 8)] == [8, 8, 3]
    assert batched([], 8) == []
    assert [len(b) for b in batched([1, 2, 3], 0)] == [1, 1, 1]


# ───────────────── preflight: the silent-truncation guard ────────────────


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _ollama(settings, monkeypatch, response=None, raises=None):
    """An ollama-backed scorer whose /api/show call is mocked."""
    settings.llm_provider = "ollama"

    def fake_post(url, **kwargs):
        assert url.endswith("/api/show"), url
        assert "/v1" not in url, "the /v1 suffix must be stripped for the native API"
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr("httpx.post", fake_post)
    return LLMScorer(settings, provider="ollama")


def test_num_ctx_is_parsed_from_the_ollama_parameters_block():
    from pitwall.scoring.llm import parse_num_ctx

    block = 'stop                           "<|im_end|>"\nnum_ctx                        8192\n'
    assert parse_num_ctx(block) == 8192
    assert parse_num_ctx('stop  "<|im_end|>"') is None
    assert parse_num_ctx("") is None
    assert parse_num_ctx("num_ctx  not-a-number") is None


def test_preflight_passes_when_the_context_window_is_big_enough(settings, monkeypatch):
    scorer = _ollama(
        settings, monkeypatch,
        FakeResponse(200, {"parameters": "num_ctx                        8192\n"}),
    )
    scorer.preflight()   # must not raise


def test_preflight_rejects_a_missing_model_with_the_remedy(settings, monkeypatch):
    scorer = _ollama(settings, monkeypatch, FakeResponse(404, {}, "not found"))
    with pytest.raises(LLMUnavailable) as exc:
        scorer.preflight()
    assert "setup_ollama.py" in str(exc.value)
    assert settings.ollama_model in str(exc.value)


def test_preflight_rejects_an_unset_context_window(settings, monkeypatch):
    """The dangerous case: Ollama would fall back to 4096 and truncate silently."""
    scorer = _ollama(
        settings, monkeypatch, FakeResponse(200, {"parameters": 'stop  "<|im_end|>"'})
    )
    with pytest.raises(LLMUnavailable) as exc:
        scorer.preflight()
    message = str(exc.value)
    assert "num_ctx" in message
    assert "4096" in message
    assert "setup_ollama.py" in message


def test_preflight_rejects_a_context_window_that_is_too_small(settings, monkeypatch):
    scorer = _ollama(
        settings, monkeypatch,
        FakeResponse(200, {"parameters": "num_ctx                        2048\n"}),
    )
    with pytest.raises(LLMUnavailable, match="num_ctx=2048"):
        scorer.preflight()


def test_preflight_reports_an_unreachable_ollama(settings, monkeypatch):
    import httpx

    scorer = _ollama(settings, monkeypatch, raises=httpx.ConnectError("refused"))
    with pytest.raises(LLMUnavailable, match="cannot reach Ollama"):
        scorer.preflight()


def test_preflight_is_a_no_op_for_cloud_providers(settings, monkeypatch):
    def explode(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("preflight must not probe the network for cloud providers")

    monkeypatch.setattr("httpx.post", explode)
    LLMScorer(settings, provider="zai").preflight()
    LLMScorer(settings, provider="deepseek").preflight()


def test_preflight_is_a_no_op_when_a_client_is_injected(settings, monkeypatch):
    """Mocked tests must never touch the network."""
    def explode(*a, **k):  # pragma: no cover
        raise AssertionError("preflight must not probe when a client was supplied")

    monkeypatch.setattr("httpx.post", explode)
    LLMScorer(settings, provider="ollama", client=FakeClient([])).preflight()


def test_the_required_context_scales_with_the_batch_size(settings):
    settings.llm_batch_size = 4
    small = LLMScorer(settings, client=FakeClient([])).required_context_tokens()
    settings.llm_batch_size = 16
    large = LLMScorer(settings, client=FakeClient([])).required_context_tokens()
    assert large > small
    assert small > settings.llm_max_tokens   # the prompt is never free


def test_the_configured_timeout_reaches_the_client(settings, monkeypatch):
    """A 90s default would time out every local batch; the setting must win."""
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    settings.llm_timeout_seconds = 900.0
    _ = LLMScorer(settings, provider="ollama").client
    assert captured["timeout"] == 900.0
    assert captured["base_url"] == settings.ollama_base_url
