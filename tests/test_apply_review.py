"""REVIEW.md — the page that answers "do I have to check this letter?"

Two things are defended. The first is that the file is ALWAYS written: before it
existed the only human-readable artefact was a block appended under the signature,
written only when something was already wrong, so a clean letter shipped with no
statement that it was clean and a flagged one had to be edited before it could be
sent.

The second is that the headline is honest. It is the one line Simon reads, and the
first version defaulted a folder holding only a CV to "Ready to send — nothing here
needs checking", which is the exact opposite of the truth.
"""

from __future__ import annotations

import pytest

from pitwall.apply.letter import Letter, Repair
from pitwall.apply.review import REVIEW_FILE, review_markdown, write_review
from pitwall.apply.truth import Violation


def a_letter(**kw) -> Letter:
    base = {"text": "Dear Hiring Manager,\n\nI built things.\n\nSimon Sazonov",
            "words": 312, "attempts": 1}
    return Letter(**{**base, **kw})


class FakeFact:
    def __init__(self, *, failures=(), independent=True, auditor="qwen2.5:7b", sentences=13):
        self.failures = list(failures)
        self.independent = independent
        self.auditor = auditor
        self.rounds = 1
        self.verdicts = [object()] * sentences

    @property
    def needs_review(self):
        return bool(self.failures)

    def summary(self):
        return f"{len(self.verdicts)} sentence(s), {len(self.verdicts) - len(self.failures)} supported"


class FakeVerdict:
    def __init__(self, sentence, verdict="UNSUPPORTED", span=""):
        self.sentence, self.verdict, self.span = sentence, verdict, span


# ── the headline ──


def test_a_clean_run_still_writes_a_review_file(tmp_path):
    text = review_markdown(company="Anthropic", title="Research Engineer",
                           state="clean", letter=a_letter())
    assert text.startswith("# Ready to send")
    assert "Check these" not in text
    path = write_review(tmp_path, text)
    assert path.name == REVIEW_FILE and path.read_text(encoding="utf-8") == text


def test_the_note_leads_with_check_these_when_something_is_flagged():
    text = review_markdown(
        state="review",
        letter=a_letter(violations=[Violation("SUMMARISING_CLOSE", "the closing lists qualities", "and rigour")]),
    )
    assert text.startswith("# Check these sentences")
    assert "and rigour" in text


def test_a_letter_that_must_not_be_sent_says_so_first():
    text = review_markdown(
        state="unsent",
        letter=a_letter(violations=[Violation("OVERCLAIM", "one comparison cannot settle this", "truly better")]),
    )
    assert text.startswith("# Do not send this letter yet")
    assert "DO_NOT_SEND" in text


def test_a_folder_with_no_letter_does_not_claim_to_be_ready_to_send():
    """The first version defaulted this to "clean" and a CV-only folder announced
    "Ready to send — nothing here needs checking"."""
    text = review_markdown(company="Anthropic", state="clean", letter=None)
    assert text.startswith("# CV only")
    assert "Ready to send" not in text
    assert "No letter was asked for" in text


def test_a_folder_whose_letter_stage_broke_says_where_to_look():
    text = review_markdown(letter=None, warnings=["the letter stage stopped: daily quota"])
    assert "stopped before it produced anything" in text
    assert "daily quota" in text


# ── the evidence behind the headline ──


def test_the_company_facts_are_listed_with_their_source_urls():
    text = review_markdown(
        company="Anthropic", state="clean", letter=a_letter(),
        company_research={"source": "research", "verified": True, "model": "gpt-oss-20b",
                          "fetched_at": "2026-09-27T10:00:00+00:00",
                          "urls": ["https://www.anthropic.com/engineering"]},
    )
    assert "https://www.anthropic.com/engineering" in text
    assert "their own pages" in text


def test_hand_written_company_notes_are_credited_to_simon():
    text = review_markdown(
        company="Anthropic", state="clean", letter=a_letter(),
        company_research={"source": "profile", "urls": []},
    )
    assert "profile/companies.yaml" in text


@pytest.mark.parametrize("independent", [True, False])
def test_the_note_says_whether_the_audit_was_independent(independent):
    text = review_markdown(
        state="review", letter=a_letter(),
        fact=FakeFact(failures=[FakeVerdict("I cut latency by 47%.", span="47%")],
                      independent=independent),
    )
    if independent:
        assert "reading independently" in text
    else:
        assert "ALSO WROTE THE LETTER" in text


def test_the_note_lists_what_was_repaired_automatically():
    text = review_markdown(
        state="clean",
        letter=a_letter(repairs=[
            Repair("NOT_SIGNED", "mechanical", "signed it 'Simon Sazonov'"),
            Repair("OVERCLAIM", "surgery", "rewrote one sentence",
                   before="truly better", after="better under this budget", by="ollama"),
        ]),
    )
    assert "signed it" in text
    assert "no model involved" in text
    assert "before: truly better" in text and "after:  better under this budget" in text
    assert "rewritten by ollama" in text


def test_a_banned_phrase_is_reported_even_though_it_has_no_code():
    text = review_markdown(state="review", letter=a_letter(banned=["passionate about"]))
    assert "passionate about" in text
    assert "style.yaml" in text
