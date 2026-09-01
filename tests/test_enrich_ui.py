"""The Leads page's contact vocabulary and rendering — §7.2 and §4.

§4's rule is a UI rule: "The label must always carry its evidence. Never render
a bare confidence percentage for a pattern guess." It is therefore tested here,
against the functions that actually produce what the owner reads, rather than
being left as a comment in a page file.

The page itself is exercised through Streamlit's own `AppTest`, which runs the
script for real. That catches the class of breakage a unit test cannot — an
import that moved, a column that no longer exists in the frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "app") not in sys.path:
    sys.path.insert(0, str(ROOT / "app"))

from _explain import (  # noqa: E402
    CONTACT_ORDER,
    CONTACT_STATUSES,
    contact_help,
    contact_label,
    contact_summary,
    contact_tone,
    evidence_line,
)

PEOPLE_PAGE = ROOT / "app" / "pages" / "3_👤_People.py"


# ───────────────────────────── the vocabulary ────────────────────────────


def test_every_status_the_database_can_hold_has_a_label():
    from pitwall.enrich import STATUSES

    assert set(CONTACT_STATUSES) == set(STATUSES)
    assert CONTACT_ORDER == ["verified", "probable", "guess", "not_found"]


def test_every_status_explains_itself_in_plain_english():
    for key in CONTACT_ORDER:
        what = contact_help(key)
        assert len(what) > 40, f"{key} needs a real explanation"
        # The vocabulary is for someone who does not know the internals.
        for jargon in ("MX", "SMTP", "regex", "heuristic", "n="):
            assert jargon not in what


def test_the_labels_are_words_not_internal_names():
    assert "Confirmed" in contact_label("verified")
    assert "Likely" in contact_label("probable")
    assert "Guess" in contact_label("guess")
    assert "Nothing found" in contact_label("not_found")


def test_an_unknown_status_falls_back_to_nothing_found():
    """Better to under-claim than to render a status the app cannot justify."""
    assert contact_label("something_new") == contact_label("not_found")
    assert contact_label(None) == contact_label("not_found")
    assert contact_tone("something_new") == "faint"


def test_the_tones_run_green_amber_grey_and_never_red():
    """An unconfirmed address is not a failure, it is an honest result — and
    red in this theme means something is broken."""
    tones = [contact_tone(k) for k in CONTACT_ORDER]
    assert tones == ["ok", "warn", "dim", "faint"]
    assert "fail" not in tones


# ──────────────── §4: the label always carries its evidence ──────────────


def test_a_pattern_guess_is_rendered_with_its_sample_size():
    out = contact_summary("probable", {"pattern": "first.last", "sample_size": 3})
    assert "Likely" in out
    assert "pattern first.last (n=3)" in out


def test_n_equals_one_and_n_equals_five_never_read_the_same():
    """§2.3, in the exact words of the brief."""
    one = contact_summary("guess", {"pattern": "first.last", "sample_size": 1})
    five = contact_summary("probable", {"pattern": "first.last", "sample_size": 5})
    assert one != five
    assert "(n=1)" in one and "(n=5)" in five


def test_a_confirmed_address_says_where_it_was_published():
    out = contact_summary(
        "verified",
        {"corroborating_url": "https://alpine.com/team", "corroborating_domain": "alpine.com"},
    )
    assert "Confirmed" in out
    assert "published: alpine.com" in out


def test_no_status_is_ever_rendered_as_a_percentage():
    """A number invites the owner to trust a hypothesis (§4)."""
    for evidence in (
        {"pattern": "first.last", "sample_size": 2, "pattern_agreement": 0.75},
        {"corroborating_url": "https://x/y"},
        {},
    ):
        for status in CONTACT_ORDER:
            out = contact_summary(status, evidence)
            assert "%" not in out
            assert "0.75" not in out


def test_the_ui_and_the_csv_say_the_same_thing_about_a_finding():
    """One evidence renderer, not two that can drift apart."""
    from pitwall.export import evidence_text

    evidence = {"pattern": "flast", "sample_size": 2, "mx_host": "Google Workspace"}
    assert evidence_line(evidence) == evidence_text(evidence)


def test_an_empty_evidence_dict_degrades_to_the_label_alone():
    assert contact_summary("guess", {}) == contact_label("guess")
    assert contact_summary("guess", None) == contact_label("guess")


# ──────────────────────────── the page itself ────────────────────────────


@pytest.fixture()
def app_root(project, monkeypatch, tmp_path):
    """Point the app at a throwaway project root, never the owner's database.

    `st.cache_resource` and `st.cache_data` are process-wide and survive an
    `AppTest` run, so `_shared.settings()` from an earlier test would otherwise
    hand this one the previous test's database — which looks exactly like a
    page that failed to render.
    """
    import streamlit as st

    from pitwall.config import reload_settings
    from pitwall.db import close_all

    monkeypatch.setenv("DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    # The page polls Hunter on load (§5.1), and `page_setup` reads the real
    # `.env` rather than the test Settings. Without this the suite would make a
    # live call against the owner's own account the moment they configured a
    # key — which the network guard caught, and which is why it exists.
    monkeypatch.setenv("HUNTER_API_KEY", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    st.cache_resource.clear()
    st.cache_data.clear()
    close_all()
    reload_settings()
    yield project
    st.cache_resource.clear()
    st.cache_data.clear()
    close_all()
    reload_settings()


def _run_page(path: Path):
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(path), default_timeout=60).run()


def test_the_people_page_runs_without_error(app_root):
    at = _run_page(PEOPLE_PAGE)
    assert not at.exception, [e.value for e in at.exception]


def test_the_people_page_shows_the_empty_state_with_no_data(app_root):
    at = _run_page(PEOPLE_PAGE)
    text = " ".join(str(m.value) for m in at.info)
    assert "No people found yet" in text


def test_the_people_page_renders_the_table_and_the_email_column(app_root, tmp_path):
    """The whole point of §7.2 item 1: a checkbox column and an email column on
    the existing grid, without disturbing what was already there."""
    from pitwall.config import get_settings
    from pitwall.db import close_all, open_repos

    settings = get_settings()
    repos = open_repos(settings)
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "jane-doe", "profile_url": "https://x/in/jane-doe",
        "full_name": "Jane Doe", "current_company": "Alpine", "current_title": "Aero",
    })
    repos.conn.execute(
        "INSERT INTO scores (candidate_id, company_id, final_score, heuristic_score,"
        " verdict, prompt_version) VALUES (?,1,88,70,'strong',?)",
        (cid, settings.prompt_version),
    )
    repos.contacts.upsert(
        cid, "jane.doe@alpine.com", status="probable", source="pattern",
        evidence={"pattern": "first.last", "sample_size": 3}, mx_valid=True,
    )
    close_all()

    at = _run_page(PEOPLE_PAGE)
    assert not at.exception, [e.value for e in at.exception]

    frame = at.dataframe[-1].value
    assert "Email" in frame.columns
    assert frame["Email"].iloc[0] == contact_label("probable")
    # The address itself is NOT in the grid — its evidence cannot fit beside it
    # there, and §4 does not allow a label without one.
    assert "jane.doe@alpine.com" not in frame.to_csv()


def test_the_export_checkbox_defaults_to_excluding_addresses(app_root):
    """§6.1: silent export is how this data leaks."""
    from pitwall.config import get_settings
    from pitwall.db import close_all, open_repos

    settings = get_settings()
    repos = open_repos(settings)
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "a", "profile_url": "https://x/in/a", "full_name": "A B",
        "current_company": "Alpine",
    })
    repos.conn.execute(
        "INSERT INTO scores (candidate_id, company_id, final_score, prompt_version)"
        " VALUES (?,1,88,?)", (cid, settings.prompt_version)
    )
    close_all()

    at = _run_page(PEOPLE_PAGE)
    boxes = {c.label: c for c in at.checkbox}
    assert "Include email addresses" in boxes
    assert boxes["Include email addresses"].value is False


def test_the_find_emails_button_is_absent_until_someone_is_selected(app_root):
    """§0.2: a manual tool on a manual selection. There is no way to press it
    for the whole table because it is not there for the whole table."""
    at = _run_page(PEOPLE_PAGE)
    labels = [b.label for b in at.button]
    assert not any("Find emails" in label for label in labels)
