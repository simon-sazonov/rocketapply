"""The cover letter as a PDF and DOCX on the CV's letterhead.

Three promises: the letterhead is the CV's own (so the two cannot drift), the
words are exactly the gated letter's (layout only, no rewriting), and a
letter marked DO_NOT_SEND never becomes a finished-looking PDF.
"""

from __future__ import annotations

import pathlib

import pytest

from pitwall.apply.families import signal_for
from pitwall.apply.history import read
from pitwall.apply.layout import Frame
from pitwall.apply.loader import load_profile
from pitwall.apply.render_letter import letter_blocks, render_letter_files
from pitwall.apply.render_pdf import render_cv
from pitwall.apply.selector import build_config

LETTER = """Dear Hiring Manager,

I built a reinforcement-learning environment for ICU sepsis treatment.

What draws me to this role is the combination of research and engineering.

Kind regards,
Simon Sazonov"""


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def frame():
    return Frame.load()


def test_paragraphs_split_on_blank_lines_and_keep_the_sign_off_together():
    blocks = letter_blocks(LETTER)
    assert blocks[0] == ["Dear Hiring Manager,"]
    assert blocks[-1] == ["Kind regards,", "Simon Sazonov"]
    assert len(blocks) == 4
    assert letter_blocks("") == []


def test_the_letterhead_is_the_cvs_own(profile, frame, tmp_path):
    cv = render_cv(profile, frame, build_config(profile, frame, signal_for("data_science")),
                   tmp_path / "cv.pdf")
    letter = render_letter_files(tmp_path, "X", LETTER, profile=profile, frame=frame,
                                 company="Apex", title="ML Engineer", date="1 October 2026")
    assert letter.sections["header"] == cv.sections["header"]
    assert letter.sections["contact"] == cv.sections["contact"]


def test_the_words_are_the_letters_words(profile, frame, tmp_path):
    out = render_letter_files(tmp_path, "X", LETTER, profile=profile, frame=frame)
    assert out.page_count == 1 and out.pdf.is_file() and out.docx.is_file()
    printed = "\n\n".join(out.sections["letter"][1:])     # after the date line
    assert printed == LETTER
    from docx import Document

    docx_text = "\n".join(p.text for p in Document(str(out.docx)).paragraphs)
    for line in LETTER.splitlines():
        assert line in docx_text


def test_a_long_letter_flows_onto_a_second_page_and_says_so(profile, frame, tmp_path):
    long = "\n\n".join([LETTER] * 6)
    out = render_letter_files(tmp_path, "X", long, profile=profile, frame=frame)
    assert out.page_count >= 2
    assert out.warnings


def test_history_sees_the_typeset_letter(profile, frame, tmp_path):
    import json

    folder = tmp_path / "Apex_ML"
    folder.mkdir()
    (folder / "provenance.json").write_text(json.dumps({
        "job": {"id": 7, "company": "Apex", "title": "ML"},
        "letter": {"state": "clean"},
        "llm_calls": [{"prompt_tokens": 100, "completion_tokens": 20, "cached": False},
                      {"prompt_tokens": 900, "completion_tokens": 90, "cached": True}],
    }), encoding="utf-8")
    (folder / "X_Cover_Letter.txt").write_text(LETTER, encoding="utf-8")
    render_letter_files(folder, "X", LETTER, profile=profile, frame=frame)
    generation = read(folder)
    assert generation.letter_pdf == folder / "X_Cover_Letter.pdf"
    assert generation.letter_docx == folder / "X_Cover_Letter.docx"
    assert generation.letter_path == folder / "X_Cover_Letter.txt", "the pdf is not the letter"
    assert generation.tokens == 120, "cached calls cost nothing"


def test_tidy_leaves_an_exported_folder_where_the_csv_says_it_is(tmp_path):
    import json

    from pitwall.apply.tidy import apply_plan, build_plan

    root = tmp_path / "output"
    for name in ("Apex_ML", "Apex_ML_v2"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "provenance.json").write_text(json.dumps({
            "generated_at": f"2026-09-2{len(name)}T10:00:00+00:00",
            "job": {"id": 7, "company": "Apex", "title": "ML", "prompt_version": "v8"},
            "letter": {"state": "review"},
        }), encoding="utf-8")
    exported = str((root / "Apex_ML").resolve())
    plan = build_plan(root, pinned=[exported])
    assert [g.name for g in plan.pinned] == ["Apex_ML"]
    apply_plan(plan, root)
    assert pathlib.Path(exported).is_dir(), "an exported folder must not move"
