"""A hand-edited letter goes through the gates a generated one passed.

The edit is not trusted more than the model: the same deterministic rules
decide its state, its file name and its folder, and the model's own text is
kept before anything is overwritten.
"""

from __future__ import annotations

import json

import pytest

from pitwall.apply.loader import load_profile
from pitwall.apply.review_edit import gate_text, save_edited_letter

CLEAN = """Dear Hiring Manager,

I built a reinforcement-learning environment for ICU sepsis treatment, modelling the
episode as a sparse-reward problem, and benchmarked tabular methods against DQN and PPO.

I would be glad to talk it through.

Simon Sazonov"""


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "output" / "1_ready_to_send" / "Apex_ML"
    directory.mkdir(parents=True)
    (directory / "provenance.json").write_text(json.dumps({
        "generated_at": "2026-09-29T09:00:00+00:00",
        "job": {"id": 0, "company": "Apex", "title": "ML Engineer", "prompt_version": "v8"},
        "letter": {"state": "clean", "file": "X_Apex_ML_Cover_Letter.txt"},
    }), encoding="utf-8")
    (directory / "X_Apex_ML_CV.pdf").write_bytes(b"%PDF")
    (directory / "X_Apex_ML_Cover_Letter.txt").write_text(CLEAN, encoding="utf-8")
    (directory / "REVIEW.md").write_text("# Ready to send\n", encoding="utf-8")
    return directory


def test_the_original_letter_passes_the_gates(profile):
    state, violations, banned = gate_text(CLEAN, profile=profile)
    assert (state, violations, banned) == ("clean", [], [])


def test_an_edit_that_stays_true_is_typeset_and_stays_ready(folder, profile):
    edited = CLEAN.replace("I would be glad", "I would be very glad")
    result = save_edited_letter(folder, edited, profile=profile)
    assert result.state == "clean"
    assert result.directory == folder, "still sendable, so it stays in 1_ready_to_send"
    assert result.letter_path.name == "X_Apex_ML_Cover_Letter.txt"
    assert result.letter_pdf is not None and result.letter_pdf.is_file()
    assert "very glad" in result.letter_path.read_text(encoding="utf-8")
    assert result.kept_original is not None and result.kept_original.read_text() == CLEAN
    data = json.loads((folder / "provenance.json").read_text())
    assert data["letter"]["edited"] is True and data["letter"]["state"] == "clean"


def test_an_edit_that_invents_a_number_is_caught_and_filed_as_do_not_send(folder, profile):
    edited = CLEAN.replace("benchmarked tabular methods",
                           "lifted survival by 37.5% after benchmarking tabular methods")
    result = save_edited_letter(folder, edited, profile=profile)
    assert result.state == "unsent", result.violations
    assert any("37.5" in v for v in result.violations)
    assert result.letter_path.name == "X_Apex_ML_Cover_Letter_DO_NOT_SEND.txt"
    assert result.letter_path.read_text(encoding="utf-8").startswith("DO NOT SEND")
    assert result.letter_pdf is None
    assert not list(result.directory.glob("*_Cover_Letter.pdf")), "never typeset"
    assert result.directory.parent.name == "2_needs_changes", "moved out of ready"
    assert not (result.directory / "X_Apex_ML_Cover_Letter.txt").exists(), "old name removed"
    assert "Edited by hand" in (result.directory / "REVIEW.md").read_text(encoding="utf-8")


def test_saving_the_banner_back_does_not_double_it(folder, profile):
    bad = CLEAN.replace("benchmarked", "benchmarked 43.7% more")
    first = save_edited_letter(folder, bad, profile=profile)
    assert first.state == "unsent"
    text = first.letter_path.read_text(encoding="utf-8")
    second = save_edited_letter(first.directory, text, profile=profile)
    assert second.letter_path.read_text(encoding="utf-8").count("DO NOT SEND") == 1


def test_a_folder_that_is_not_an_application_is_refused(tmp_path, profile):
    with pytest.raises(ValueError):
        save_edited_letter(tmp_path, CLEAN, profile=profile)
