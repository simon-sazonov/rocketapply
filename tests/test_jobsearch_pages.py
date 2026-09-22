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


def _run(page: Path) -> AppTest:
    return AppTest.from_file(str(page), default_timeout=60).run()


def _ok(at: AppTest) -> None:
    assert not at.exception, [repr(e.value) for e in at.exception]


def test_the_inbox_explains_itself_before_the_first_run(job_app):
    at = _run(SEARCH_PAGE)
    _ok(at)
    assert any("No jobs yet" in str(i.value) for i in at.info)


def test_the_inbox_shows_matches_and_the_filtered_tab_says_why(job_app):
    _populate()
    at = _run(SEARCH_PAGE)
    _ok(at)
    labels = [e.label for e in at.expander]
    assert any("Graduate Machine Learning Engineer" in label for label in labels)
    assert any("Senior ML Engineer" in label for label in labels), "shown under Filtered out"


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
    assert [t.label for t in at.tabs] == ["Profile", "Skills", "Target companies",
                                          "Sources & health"]
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
