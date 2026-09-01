"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).parent / "fixtures"


# ───────────────────────── the suite makes no requests ───────────────────
#
# §8 of PITWALL_V3_EMAIL_ENRICHMENT.md: "No test makes a real network call —
# not Hunter, not GitHub, not SearXNG, not DNS."
#
# Enforced at the socket, not by convention. Every adapter in this project
# takes an injectable transport, searcher, fetcher or resolver precisely so
# that this can hold, and a test that forgets to use one should fail loudly
# here rather than quietly reaching the internet on somebody's laptop — or, on
# a machine where SearXNG happens to be running, quietly passing because of it.
#
# Loopback is left open: `AppTest` and SQLite do not use it, but Streamlit's
# runtime may, and blocking it would be forbidding something that is not a
# network call in any meaningful sense.


class NetworkUsedInTest(RuntimeError):
    """A test tried to open a socket. Inject a stub instead."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    real_connect = socket.socket.connect

    def guard(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and (
            host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")
        ):
            return real_connect(self, address, *args, **kwargs)
        raise NetworkUsedInTest(
            f"a test tried to connect to {host!r}. The suite makes no network "
            "call (§8) — inject a stub searcher, transport, fetcher or MX "
            "resolver instead."
        )

    monkeypatch.setattr(socket.socket, "connect", guard)


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



# ───────────── contact enrichment, with the network stubbed out ─────────
#
# §8 of PITWALL_V3_EMAIL_ENRICHMENT.md: "No test makes a real network call —
# not Hunter, not GitHub, not SearXNG, not DNS." Every fixture below exists to
# make that structural rather than a habit: a step reaches the network only
# through an injected searcher, resolver or HTTP client, and each of these
# raises rather than connecting if a test forgets to stub one.


class StubSearcher:
    """A deterministic stand-in for the SearXNG-backed enrichment searcher.

    Keyed on a substring of the query rather than the whole string, because the
    steps build their queries from templates and asserting on the exact text
    would make every test a change-detector.
    """

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.queries: list[str] = []
        self.fail_on: set[str] = set()

    def supports_operators(self) -> bool:
        return True

    def search(self, query: str, *, max_results: int = 10):
        self.queries.append(query)
        for needle in self.fail_on:
            if needle in query:
                raise RuntimeError(f"stub searcher refusing {needle!r}")
        # Longest needle wins. A harvest key like "@alpine.com" is a substring
        # of the corroboration query `"jane.doe@alpine.com"`, so first-match
        # order would answer the corroboration search with the team page.
        matches = [(n, h) for n, h in self.pages.items() if n in query]
        if not matches:
            return []
        matches.sort(key=lambda pair: -len(pair[0]))
        return list(matches[0][1])[:max_results]


@pytest.fixture()
def searcher():
    return StubSearcher


@pytest.fixture()
def no_dns():
    """An MX resolver that fails the test if anything actually asks DNS."""

    def _resolver(domain):
        raise AssertionError(f"a test tried to resolve MX for {domain!r}")

    return _resolver


@pytest.fixture()
def mx_ok():
    """A resolver that answers as Google Workspace for every domain."""
    return lambda domain: ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]


@pytest.fixture()
def enrich_ctx(settings, repos):
    """Factory for an `EnrichmentContext` around a real (throwaway) database."""
    from pitwall.enrich.base import EnrichmentContext
    from pitwall.enrich.checks import reset_mx_cache

    reset_mx_cache()

    def _make(
        *,
        name="Jane Doe",
        company_name="Alpine",
        searcher=None,
        candidate=None,
        company=None,
        **kwargs,
    ):
        if company is None and company_name:
            repos.conn.execute(
                "INSERT OR IGNORE INTO companies (name) VALUES (?)", (company_name,)
            )
            row = repos.conn.execute(
                "SELECT * FROM companies WHERE name = ?", (company_name,)
            ).fetchone()
            company = dict(row) if row else None
        if candidate is None:
            slug = name.lower().replace(" ", "-")
            cid, _ = repos.candidates.upsert(
                {
                    "linkedin_slug": slug,
                    "profile_url": f"https://uk.linkedin.com/in/{slug}",
                    "full_name": name,
                    "current_company": company_name,
                }
            )
            candidate = repos.conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (cid,)
            ).fetchone()
            candidate = dict(candidate)
        return EnrichmentContext(
            candidate=candidate, settings=settings, repos=repos,
            company=company, searcher=searcher, **kwargs,
        )

    yield _make
    reset_mx_cache()


def serp(url, title="", snippet=""):
    """One SERP hit, for the stub searcher's pages."""
    return SearchHit(0, url, title, snippet, {})


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
