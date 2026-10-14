"""The job-search fixture RocketApply's app tests run on.

A throwaway root and database (`app_root`), never the owner's, with the job
config copied in, and `_populate` for one real run over the Arbeitnow fixture.
Moved here from PitWall's old job-page tests when RocketApply got its own repo.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
