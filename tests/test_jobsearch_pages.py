"""The three job-search pages render — empty, populated, and after an action.

Smoke tests through Streamlit's AppTest, on a throwaway root and database
(`app_root`), never the owner's. They check the pages do not fall over and that
the one path the spec is built around works: a job is found, saved, applied to,
and shows up on the tracker (§92).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "app" / "pages"
SEARCH_PAGE = PAGES / "7_🎯_Job_Search.py"
APPS_PAGE = PAGES / "8_📨_Applications.py"
SETUP_PAGE = PAGES / "9_🧭_Job_Setup.py"
HOME = ROOT / "app" / "Home.py"


@pytest.fixture()
def job_app(app_root, project):
    config = project / "config"
    for name in ("skills_vocab.yaml", "job_search.example.yaml", "my_skills.example.yaml"):
        shutil.copy2(ROOT / "config" / name, config / name)
    (config / "target_companies.csv").write_text(
        "name,aliases,country,cities,industry,priority,careers_url,ats_kind,ats_token,"
        "sponsorship,visa_notes,tags,active\n"
        "Apex Mobility,Apex Mobility GmbH,Germany,Munich,Automotive,A,"
        "https://apex.test/careers,,,unknown,,,true\n",
        encoding="utf-8",
    )
    return app_root


def _populate() -> None:
    """One real run over the Arbeitnow fixture, into the app's database."""
    from pitwall.config import get_settings
    from pitwall.jobsearch.run import JobSearch
    from pitwall.pipeline import Pipeline
    from pitwall.sources.jobs import arbeitnow as arbeitnow_mod

    postings = arbeitnow_mod.parse(
        json.loads((ROOT / "tests" / "fixtures" / "jobs" / "arbeitnow.json").read_text())
    )

    class Stub:
        name, kind, url, complete_listing = "arbeitnow", "job_board", "https://a.test", False

        def fetch(self, *, no_cache=False):
            return postings

    JobSearch(Pipeline(get_settings())).run(sources=[Stub()])


def _run(page: Path, **state: object) -> AppTest:
    at = AppTest.from_file(str(page), default_timeout=60)
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def _ok(at: AppTest) -> None:
    assert not at.exception, [repr(e.value) for e in at.exception]


def test_the_inbox_explains_itself_before_the_first_run(job_app):
    at = _run(SEARCH_PAGE)
    _ok(at)
    assert any("No jobs yet" in str(i.value) for i in at.info)


def test_the_inbox_shows_matches_and_the_filtered_view_says_why(job_app):
    _populate()
    at = _run(SEARCH_PAGE)
    _ok(at)
    assert any("Graduate Machine Learning Engineer" in e.label for e in at.expander)

    # One view is rendered at a time, so the rejected jobs are their own view.
    at = _run(SEARCH_PAGE, js_view="Filtered out · 4", js_role=True)
    _ok(at)
    labels = [e.label for e in at.expander]
    assert any("Senior ML Engineer" in label for label in labels)
    card = next(e for e in at.expander if "Senior ML Engineer" in e.label)
    text = " ".join(str(m.value) for m in card.markdown)
    assert "8+ years" in text and "senior" in text.lower(), "the reason it was removed"


def test_saving_a_job_puts_it_on_the_tracker(job_app):
    _populate()
    at = _run(SEARCH_PAGE)
    save = next(b for b in at.button if b.label == "⭐ Save")
    save.click().run()
    _ok(at)
    apps = _run(APPS_PAGE)
    _ok(apps)
    assert any(m.label == "Shortlisted" and m.value == "1" for m in apps.metric)


def test_the_tracker_explains_itself_when_empty(job_app):
    at = _run(APPS_PAGE)
    _ok(at)
    assert any("No applications yet" in str(i.value) for i in at.info)


def test_the_setup_page_renders_every_tab(job_app):
    _populate()
    at = _run(SETUP_PAGE)
    _ok(at)
    assert [t.label for t in at.tabs] == ["Profile", "My CV (for the AI)", "Skills",
                                          "Target companies", "Sources & health"]
    assert any("Countries I can already work in" in str(w.label) for w in at.multiselect)


def test_the_setup_page_warns_while_work_authorisation_is_blank(job_app):
    at = _run(SETUP_PAGE)
    _ok(at)
    assert any("Countries I can already work in" in str(w.value) for w in at.warning)


def test_home_mentions_the_job_search(job_app):
    _populate()
    at = _run(HOME)
    _ok(at)
    assert any("Job search" in str(m.value) for m in at.markdown)


def test_the_inbox_says_what_it_knows_and_what_it_did(job_app):
    _populate()
    at = _run(SEARCH_PAGE)
    _ok(at)
    labels = [e.label for e in at.expander]
    assert "What the search knows about you" in labels
    assert "What the last search did" in labels
    assert any("How a job is scored" in label for label in labels)


def test_the_inbox_names_what_is_still_missing(job_app):
    _populate()
    at = _run(SEARCH_PAGE)
    _ok(at)
    text = " ".join(str(c.value) for c in at.caption)
    assert "Countries you can already work in are not set" in text
    assert "No CV for the AI" in text


def test_quick_add_puts_a_company_on_the_list(job_app):
    import _jobs

    _populate()
    added = _jobs.quick_add("", "Zalando")
    assert any("Zalando" in line for line in added)
    from pitwall.jobsearch.companies import load_targets

    assert "Zalando" in [t.name for t in load_targets()[0]]


def test_quick_add_says_so_when_a_skill_is_not_in_the_vocabulary(job_app):
    import _jobs

    assert any("vocabulary" in line for line in _jobs.quick_add("Underwater Basket Weaving", ""))
    assert any("PyTorch" in line for line in _jobs.quick_add("pytorch", ""))
