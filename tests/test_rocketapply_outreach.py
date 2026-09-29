"""The note to a person at a company you applied to — and the links to find them.

Drafts only: a test asserts there is no way to send one. The gates are the
letter's plus the three a public note adds (length, contact details, claiming
an application before it is one).
"""

from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qs, urlparse

import pytest

from pitwall.apply import outreach
from pitwall.apply.llm import CAPABLE_ONLY, TASK_ROUTING, TaskRouter
from pitwall.apply.loader import load_profile
from pitwall.apply.outreach import (
    LIMIT,
    check_note,
    draft_note,
    note_facts,
    notes_for,
    save_note,
)
from pitwall.apply.truth import build_truth
from pitwall.jobsearch.linkedin_links import people_search, presets

from .test_apply_pipeline import StubClient  # noqa: TID252

PERSON = {"full_name": "Rita Recruits", "headline": "Technical Recruiter at Apex",
          "profile_url": "https://www.linkedin.com/in/rita-recruits", "group": "recruiter"}
APP = {"id": 1, "company_name": "Apex", "title": "ML Engineer", "status": "IN_PROCESS"}
GOOD = "Hi Rita — I'm interested in the ML Engineer role at Apex. I built an RL environment for ICU sepsis treatment; glad to connect."


@pytest.fixture(scope="module")
def profile():
    return load_profile()


# ── the gates ──


def test_a_good_note_passes(profile):
    assert len(GOOD) <= LIMIT
    assert check_note(GOOD, truth=build_truth(profile)) == []


@pytest.mark.parametrize("text, failure", [
    ("x" * 201, "too long"),
    ("Hi Rita, write to me at simon@example.com", "contact details"),
    ("Hi Rita, see https://github.com/me", "contact details"),
    ("Hi Rita, I've applied for the ML role.", "says you applied"),
    ("", "empty"),
])
def test_each_gate_names_what_is_wrong(text, failure):
    assert any(failure in f for f in check_note(text))


def test_saying_you_applied_is_fine_once_you_have():
    assert check_note("Hi Rita, I've applied for the ML role.", applied=True) == []


def test_banned_phrases_are_caught_whatever_the_spacing():
    assert check_note("I am  passionate   about ML", banned=["passionate about"]) == \
        ["banned phrase: passionate about"]


# ── drafting ──


def _router(*replies):
    client = StubClient(*replies)
    return TaskRouter(clients={"groq": client}), client


def test_two_clean_variants_come_back_in_one_call(profile):
    router, client = _router({"variants": [{"text": GOOD}, {"text": GOOD.replace("Hi", "Hello")}]})
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert draft.variants == [GOOD, GOOD.replace("Hi", "Hello")]
    assert draft.checks == [[], []]
    assert draft.attempts == 1 and len(client.prompts) == 1
    prompt = client.prompts[0]
    assert "Rita Recruits" in prompt and "applied: no" in prompt and "recruiter" in prompt


def test_a_failing_draft_is_retried_once_with_the_rule_named(profile):
    long = "Hi Rita, " + "really " * 40
    router, client = _router({"variants": [{"text": long}, {"text": GOOD}]},
                             {"variants": [{"text": GOOD}, {"text": GOOD}]})
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert draft.attempts == 2 and draft.checks == [[], []]
    assert "too long" in client.prompts[1]


def test_a_draft_that_still_fails_comes_back_with_its_failures(profile):
    router, _ = _router({"variants": [{"text": "x" * 300}, {"text": GOOD}]})
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert draft.attempts == 2
    assert draft.checks[0] and "too long" in draft.checks[0][0]


def test_the_facts_are_the_ones_the_letter_was_planned_on(profile, tmp_path):
    first = profile.evidence[-1]
    (tmp_path / "provenance.json").write_text(json.dumps(
        {"narrative_plan": {"evidence_order": [first.id]}}), encoding="utf-8")
    facts = note_facts(profile, {"generation_dir": str(tmp_path)})
    assert facts[0] == (first.one_line_hook or first.facts[0]).strip()
    assert note_facts(profile, {}), "without a plan, the strongest evidence stands in"


def test_notes_are_prose_so_they_never_fall_back_to_a_small_local_model():
    assert TASK_ROUTING["outreach_note"] == ("groq",)
    assert "outreach_note" in CAPABLE_ONLY


def test_there_is_no_way_to_send_a_note():
    source = pathlib.Path(outreach.__file__).read_text(encoding="utf-8")
    for needle in ("smtplib", "requests.post", "httpx.post", "voyager", "def send"):
        assert needle not in source, needle


# ── storage ──


def test_saving_again_replaces_the_note_for_that_person(repos):
    conn = repos.conn
    conn.execute("INSERT INTO applications (id, company_name, title) VALUES (1, 'Apex', 'ML')")
    first = save_note(conn, 1, PERSON, "one", model="groq/x", tokens=100)
    again = save_note(conn, 1, PERSON, "two", edited=True, tokens=50)
    assert first == again
    [note] = notes_for(conn, 1)
    assert (note["text"], note["edited"], note["model"], note["tokens"]) == \
        ("two", 1, "groq/x", 150)


# ── the links ──


def test_a_people_search_link_carries_its_words_and_nothing_else():
    url = people_search("Apex  recruiter")
    parsed = urlparse(url)
    assert parsed.netloc == "www.linkedin.com" and parsed.path == "/search/results/people/"
    assert parse_qs(parsed.query)["keywords"] == ["Apex recruiter"]


def test_presets_cover_every_group(profile):
    rows = presets("Anthropic", "Research Engineer, Code RL (Reinforcement Learning)",
                   profile=profile)
    assert {r.group for r in rows} == {"recruiter", "hiring", "peer", "alumni"}
    assert any(r.label == "Anthropic Research Engineer" for r in rows)
    assert any("Nova University of Lisbon" in r.label for r in rows)
    assert presets("", "x") == []


# ── runs ──


def test_a_run_of_another_kind_does_not_block(repos):
    run_id = repos.runs.start({}, command="run")
    assert repos.runs.active() is not None
    assert repos.runs.active(commands=("job-search",)) is None
    assert repos.runs.active(commands=("run",))["id"] == run_id
