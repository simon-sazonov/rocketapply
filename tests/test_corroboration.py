"""Corroboration scoring — §1.4.

285 of 384 people in the real database were surfaced by exactly one query, so
agreement across sources is a strong and entirely unused quality signal. Two
near-identical queries finding the same profile says little; `profile_exact` and
`github_discovery` both finding them says a lot, because those look at different
corners of the web.

**The weights ship at zero.** The brief says "do not guess the weight", and §1.3
says the eval set has to be re-labelled against SearXNG snippet shapes before
any number swept from it means anything. So this file mostly asserts that the
mechanism works when switched on and changes nothing while it is off.
"""

from __future__ import annotations

import pytest
import yaml

from pitwall.config import Settings
from pitwall.inputs import sync_to_db
from pitwall.pipeline import Pipeline
from pitwall.scoring import heuristics
from pitwall.scoring.heuristics import score_candidate

from .conftest import FIXTURES  # noqa: TID252

ROOT = FIXTURES.parents[1]


def cand(**kw):
    base = {
        "id": 1,
        "profile_url": "https://www.linkedin.com/in/jane-doe",
        "full_name": "Jane Doe",
        "headline": "Data Scientist at McLaren Racing",
        "location": "Woking, England",
        "evidence_count": 1,
        "template_count": 1,
        "current_company": "McLaren Racing",
    }
    base.update(kw)
    return base


TITLE = "Jane Doe - Data Scientist - McLaren Racing | LinkedIn"
SNIPPET = "Data Scientist at McLaren Racing · Woking, England, United Kingdom"


def score(candidate, company, role, **kw):
    return score_candidate(candidate, company, role, serp_title=TITLE,
                           serp_snippet=SNIPPET, **kw)


# ──────────────────────────── off by default ─────────────────────────────


def test_the_weights_ship_at_zero():
    """`do not guess the weight` — §1.4."""
    assert heuristics.W_CORROBORATION_QUERY == 0.0
    assert heuristics.W_CORROBORATION_TEMPLATE == 0.0
    s = Settings(_env_file=None)
    assert s.corroboration_query_bonus == 0.0
    assert s.corroboration_template_bonus == 0.0


def test_the_shipped_settings_file_agrees():
    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    corr = data["scoring"]["corroboration"]
    assert corr["per_query"] == 0 and corr["per_template"] == 0


def test_scoring_is_unchanged_while_the_bonus_is_off(company, role):
    """The 260 tests are the guard rail; this is the same guarantee, stated."""
    one = score(cand(evidence_count=1, template_count=1), company, role)
    many = score(cand(evidence_count=2, template_count=2), company, role)
    assert one.score == many.score


# ───────────────────────── the mechanism, when on ────────────────────────


def test_extra_queries_earn_a_bonus_when_weighted(company, role):
    w = {"query": 2.0, "template": 0.0, "max": 12.0}
    base = score(cand(evidence_count=1), company, role, weights=w)
    two = score(cand(evidence_count=2), company, role, weights=w)
    assert two.score == pytest.approx(base.score + 2.0)


def test_extra_templates_are_weighted_separately(company, role):
    """A different template is stronger evidence than a different query string."""
    w = {"query": 1.0, "template": 4.0, "max": 12.0}
    same_template = score(cand(evidence_count=2, template_count=1), company, role, weights=w)
    two_templates = score(cand(evidence_count=2, template_count=2), company, role, weights=w)
    assert two_templates.score == pytest.approx(same_template.score + 4.0)


def test_the_bonus_is_capped(company, role):
    """Isolated from the older +5 step by comparing the same candidate on and off."""
    w = {"query": 5.0, "template": 5.0, "max": 6.0}
    person = cand(evidence_count=9, template_count=9)
    on = score(person, company, role, weights=w)
    off = score(person, company, role)
    assert on.score - off.score == pytest.approx(6.0)


def test_a_single_source_earns_nothing(company, role):
    w = {"query": 5.0, "template": 5.0, "max": 12.0}
    assert score(cand(evidence_count=1, template_count=1), company, role, weights=w).score == (
        score(cand(evidence_count=1, template_count=1), company, role).score
    )


def test_the_bonus_explains_itself(company, role):
    w = {"query": 2.0, "template": 3.0, "max": 12.0}
    result = score(cand(evidence_count=3, template_count=2), company, role, weights=w)
    reason = next(r for r in result.reasons if "corroborated" in r)
    assert "3 queries" in reason and "2 template" in reason


def test_the_older_all_or_nothing_bonus_is_untouched(company, role):
    """+5 at three distinct queries predates this and stays where it was."""
    two = score(cand(evidence_count=2), company, role)
    three = score(cand(evidence_count=3), company, role)
    assert three.score - two.score == pytest.approx(heuristics.W_EVIDENCE)


# ─────────────────────── the counts come from the data ───────────────────


def test_distinct_templates_are_counted_alongside_distinct_queries(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    conn = repos.conn
    comp = repos.companies.all()[0]
    rol = repos.roles.all()[0]
    for qid, template in ((1, "profile_exact"), (2, "profile_exact"), (3, "github_discovery")):
        conn.execute(
            "INSERT INTO queries (id, company_id, role_id, template_id, query_text,"
            " query_hash, status) VALUES (?,?,?,?,?,?, 'done')",
            (qid, comp["id"], rol["id"], template, f"q{qid}", f"h{qid}"),
        )
    cid, _ = repos.candidates.upsert(
        {"linkedin_slug": "jane", "profile_url": "https://www.linkedin.com/in/jane"}
    )
    for i, qid in enumerate((1, 2, 3), start=1):
        conn.execute(
            "INSERT INTO search_results (id, query_id, provider, rank, url, canonical_url)"
            " VALUES (?,?, 'searxng', 0, ?, ?)",
            (i, qid, "https://www.linkedin.com/in/jane", "https://linkedin.com/in/jane"),
        )
        repos.candidates.link_source(cid, i)

    repos.candidates.refresh_evidence_count(cid)
    row = repos.candidates.get_by_slug("jane")
    assert row["evidence_count"] == 3, "three distinct queries"
    assert row["template_count"] == 2, "but only two distinct templates"


def test_the_column_survives_a_migration(settings, repos):
    cols = {r["name"] for r in repos.conn.execute("PRAGMA table_info(candidates)")}
    assert "template_count" in cols


def test_the_pipeline_passes_the_configured_weights_through(settings, repos, company, role):
    sync_to_db(repos.conn, [company], [role])
    settings.corroboration_query_bonus = 3.0
    pipe = Pipeline(settings, repos)
    assert pipe.settings.corroboration_weights()["query"] == 3.0


def test_the_eval_worksheet_carries_the_template_count():
    """Otherwise the per-template column of the sweep is measuring nothing."""
    source = (ROOT / "scripts" / "eval_scoring.py").read_text(encoding="utf-8")
    assert '"template_count"' in source
