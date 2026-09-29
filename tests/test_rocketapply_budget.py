"""How many applications fit in what is left of today's Groq quota.

The cap RocketApply puts on a batch comes from here, so the arithmetic has to
be right and has to say where its numbers came from.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from pitwall.apply import budget
from pitwall.apply.budget import FALLBACK, fits_today, key_room, per_job_tokens, run_mode
from pitwall.apply.keys import fingerprint

KEYS = ["gsk_test_key_one_aaaa", "gsk_test_key_two_bbbb"]


def _settings(keys=KEYS):
    return SimpleNamespace(provider_keys=lambda provider: list(keys))


def _usage(path, spent: dict[str, int], *, parked: str | None = None):
    now = time.time()
    rows = []
    for key, tokens in spent.items():
        row = {"key": fingerprint(key), "spend": [[now - 3600, tokens]],
               "parked_until": 0.0, "parked_reason": ""}
        if parked == key:
            row.update(parked_until=now + 600, parked_reason="daily ceiling")
        rows.append(row)
    # One old spend, outside the rolling day: it must not count.
    rows[0]["spend"].append([now - 90_000, 150_000])
    path.write_text(json.dumps({"groq": rows}), encoding="utf-8")
    return path


def _run(root, name, calls, at="2026-09-29T08:00:00+00:00"):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "provenance.json").write_text(json.dumps({
        "generated_at": at, "job": {"id": 1}, "llm_calls": calls}), encoding="utf-8")


def _call(stage, tokens, cached=False):
    return {"stage": stage, "prompt_tokens": tokens - 100, "completion_tokens": 100,
            "cached": cached}


def test_a_run_is_classified_by_its_model_calls():
    assert run_mode([_call("job_analysis", 2600)]) == "cv"
    assert run_mode([_call("job_analysis", 1), _call("letter_write", 1)]) == "cv_letter"
    assert run_mode([_call("letter_write", 1), _call("fact_check", 1)]) == "cv_letter_audit"


def test_cost_per_job_is_the_80th_percentile_once_there_are_five_runs(tmp_path):
    out = tmp_path / "output"
    assert per_job_tokens("cv_letter", output=out)[0] == FALLBACK["cv_letter"]
    for i, tokens in enumerate([10_000, 12_000, 14_000, 16_000, 30_000]):
        _run(out / "1_ready_to_send", f"A{i}",
             [_call("job_analysis", 2_000), _call("letter_write", tokens - 2_000)])
    _run(out, "cached", [_call("letter_write", 9_000, cached=True)])   # costs nothing: ignored
    tokens, samples, basis = per_job_tokens("cv_letter", output=out)
    assert (tokens, samples) == (16_000, 5)
    assert "p80 of 5" in basis


def test_room_is_per_key_over_a_rolling_day_and_a_parked_key_has_none(tmp_path):
    usage = _usage(tmp_path / "usage.json", {KEYS[0]: 50_000, KEYS[1]: 20_000}, parked=KEYS[1])
    rooms = key_room(_settings(), usage_path=usage, output=tmp_path / "none")
    assert [(r.day_used, r.room) for r in rooms] == [(50_000, 150_000), (20_000, 0)]
    assert rooms[1].reason == "daily ceiling"
    assert all(r.key.startswith("...") for r in rooms), "fingerprints, never keys"


def test_fits_today_divides_room_less_margin_by_cost(tmp_path):
    usage = _usage(tmp_path / "usage.json", {KEYS[0]: 0, KEYS[1]: 100_000})
    b = fits_today("cv", settings=_settings(), usage_path=usage, output=tmp_path / "none")
    assert b.room == 300_000
    assert b.usable == 270_000
    assert b.fits == 270_000 // FALLBACK["cv"]
    assert "10% margin" in b.basis


def test_each_new_employer_reserves_room_for_its_research(tmp_path, monkeypatch):
    usage = _usage(tmp_path / "usage.json", {KEYS[0]: 200_000 - 50_000, KEYS[1]: 200_000})
    monkeypatch.setattr(budget, "_research_reserve", lambda ids, s: ["Apex", "Beta"])
    import pitwall.apply.jobs as jobs

    monkeypatch.setattr(jobs, "companies_for",
                        lambda ids, conn=None: {1: "Apex", 2: "Apex", 3: "Beta", 4: "Cold"})
    b = fits_today("cv_letter", job_ids=[1, 2, 3, 4], settings=_settings(), usage_path=usage,
                   output=tmp_path / "none")
    # 45,000 usable: 18,200 + 4,000 (Apex) = 22,200; +18,200 = 40,400; Beta would
    # need 18,200 + 4,000 more. Two fit.
    assert b.usable == 45_000
    assert b.fits == 2
    assert b.reserve == budget.RESEARCH_TOKENS
    assert b.unresearched == 2


def test_no_keys_means_nothing_fits(tmp_path):
    b = fits_today("cv", settings=_settings(keys=[]), usage_path=None, output=tmp_path)
    assert (b.room, b.fits) == (0, 0)


@pytest.mark.parametrize("mode", ["cv", "cv_letter", "cv_letter_audit"])
def test_every_mode_has_a_label_and_an_estimate(mode):
    assert budget.MODE_LABELS[mode] and FALLBACK[mode] > 0
