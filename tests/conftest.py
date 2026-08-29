"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A throwaway project root with its own `config/`.

    Tests must never read (or write) the real `config/companies.csv` — the
    pipeline re-syncs the CSVs on every stage, so a test that shares them would
    silently pick up the live 49-company dataset.
    """
    import shutil

    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    # settings.yaml is copied too, so tests see the SHIPPED tuned values
    # (thresholds, weights, bands) rather than the code defaults.
    for name in ("settings.yaml", "query_templates.yaml", "negative_keywords.yaml"):
        shutil.copy2(ROOT / "config" / name, config / name)
    (config / "companies.csv").write_text(
        "name,aliases,negative_keywords,linkedin_slug,domain,series,country,tier,active\n"
        "McLaren Racing,McLaren F1|McLaren Applied|MTC,"
        "Health Care|Automotive|hospital|Macomb,,,F1,UK,1,true\n",
        encoding="utf-8",
    )
    (config / "roles.csv").write_text(
        "title,aliases,family,seniority,keywords,priority,active\n"
        "Data Scientist,Data Science|ML Engineer,data_science,graduate|junior|mid,"
        "Monte Carlo|tyre degradation,1,true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PITWALL_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture()
def settings(project, tmp_path, monkeypatch):
    """A Settings pointed at a throwaway project root, database and cache."""
    from pitwall.config import Settings

    for key in ("SERPER_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return Settings(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        search_providers=["ddgs"],
        llm_provider="zai",
        # One keyless cloud provider and nothing after it. Without this the v2
        # chain would fall through to `ollama`, and on a machine where Ollama is
        # actually running (this project's own setup) the suite would start
        # making real inference calls.
        llm_provider_chain=["zai"],
        llm_fallback="none",
        max_queries_per_run=50,
        _env_file=None,
    )


@pytest.fixture()
def repos(settings):
    from pitwall.db import close_all, open_repos

    r = open_repos(settings)
    yield r
    close_all()


@pytest.fixture()
def company():
    from pitwall.models import Company

    return Company(
        name="McLaren Racing",
        aliases="McLaren F1|McLaren Applied|MTC",
        negative_keywords="Health Care|Automotive|hospital|Macomb",
        series="F1",
        country="UK",
        tier=1,
    )


@pytest.fixture()
def role():
    from pitwall.models import Role

    return Role(
        title="Data Scientist",
        aliases="Data Science|ML Engineer",
        family="data_science",
        seniority="graduate|junior|mid",
        keywords="Monte Carlo|tyre degradation",
        priority=1,
    )


@pytest.fixture()
def templates(project):
    from pitwall.query_builder import load_templates

    return load_templates(project / "config" / "query_templates.yaml")


@pytest.fixture()
def write_inputs(project):
    """Rewrite the throwaway `config/*.csv` for a test that needs other rows."""
    from pitwall.inputs import save_companies, save_roles

    def _write(companies, roles):
        save_companies(companies, project / "config" / "companies.csv")
        save_roles(roles, project / "config" / "roles.csv")

    return _write


# ─────────────── a pipeline with the network stubbed out ────────────────

from pitwall.search.base import SearchHit  # noqa: E402

STUB_HITS = [
    SearchHit(0, "https://uk.linkedin.com/in/jane-doe",
              "Jane Doe - Data Scientist - McLaren Racing | LinkedIn",
              "Data Scientist at McLaren Racing · Woking, England, United Kingdom", {}),
    SearchHit(1, "https://www.linkedin.com/in/david-brooks",
              "David Brooks - Data Analyst - McLaren Health Care | LinkedIn",
              "Macomb, Michigan, United States", {}),
    SearchHit(2, "https://www.linkedin.com/in/luca-rossi",
              "Luca Rossi - Strategy Consultant | LinkedIn",
              "Ex-McLaren Racing data scientist, now at Bain", {}),
    SearchHit(3, "https://www.linkedin.com/posts/someone_activity-1",
              "A post about F1 data", "not a person", {}),
    SearchHit(4, "https://uk.linkedin.com/in/Jane-Doe/",   # same person, mirror host
              "Jane Doe - Senior Data Scientist - McLaren Racing | LinkedIn",
              "Senior Data Scientist at McLaren Racing, working on tyre degradation and "
              "Monte Carlo race simulation · Woking, England, United Kingdom", {}),
]


class StubProvider:
    """A deterministic stand-in for a real search provider."""

    name = "ddgs"

    def __init__(self, hits=None):
        self.hits = STUB_HITS if hits is None else hits
        self.calls = 0

    def supports_operators(self) -> bool:
        return False

    def remaining_quota(self):
        return None

    def search(self, query, *, max_results=20, region="wt-wt"):
        """First query returns the short Jane headline, later queries the long one.

        Two URLs that canonicalize identically inside ONE result page are the
        same row and collapse there (UNIQUE(query_id, canonical_url)); the
        longest-wins merge only has anything to do across different queries.
        """
        self.calls += 1
        page = list(self.hits)
        if self.calls > 1 and len(page) >= 5:
            page = [page[4], *page[1:4]]
        else:
            page = page[:4]
        return page[:max_results]



@pytest.fixture()
def pipeline(settings, repos, monkeypatch, company, role):
    """A Pipeline whose provider chain is a stub — no test makes a request."""
    from pitwall.inputs import sync_to_db
    from pitwall.pipeline import Pipeline

    sync_to_db(repos.conn, [company], [role])
    provider = StubProvider()
    monkeypatch.setattr("pitwall.pipeline.build_chain", lambda *a, **k: [provider])
    monkeypatch.setattr("pitwall.query_builder.build_chain", lambda *a, **k: [provider],
                        raising=False)
    pipe = Pipeline(settings, repos)
    pipe.stub = provider
    return pipe
